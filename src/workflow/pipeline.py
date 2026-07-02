"""
Main workflow pipeline for paper-to-poster generation
"""

import argparse
import os
import sys
import json
import time
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Callable, Dict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.state.poster_state import create_state, PosterState
from src.tools.layout_api import LayoutTemplates
from src.template_extraction.block_template_registry import get_block_template_info, is_block_template_id
from src.config.poster_config import load_config
from src.utils.style_options import (
    available_poster_styles,
    available_visual_densities,
    normalize_poster_style,
    normalize_visual_density,
)
from utils.src.logging_utils import log_agent_info, log_agent_success, log_agent_error

env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(env_path, override=False)


def resolve_poster_dimensions(layout_template: str, width: float | None, height: float | None) -> tuple[float, float]:
    """Resolve canvas size from the selected template.

    Built-in templates keep the historical 54 x 36 landscape default. Block
    templates use their own aspect ratio and orientation, so current cluster_*
    templates default to a portrait canvas.
    """
    if is_block_template_id(layout_template):
        info = get_block_template_info(layout_template)
        if not info:
            raise ValueError(f"Unknown block template: {layout_template}")
        aspect_ratio = float(info["aspect_ratio"])
        orientation = info["orientation"]
        recommended = info["recommended_canvas_size"]

        if width is None and height is None:
            width = float(recommended["width"])
            height = float(recommended["height"])
        elif width is None:
            height = float(height)
            width = height * aspect_ratio
        elif height is None:
            width = float(width)
            height = width / max(aspect_ratio, 1e-6)
        else:
            width = float(width)
            height = float(height)
            requested_orientation = "portrait" if width < height else "landscape"
            if requested_orientation != orientation:
                raise ValueError(
                    f"Template {layout_template} is {orientation}, but requested canvas is {requested_orientation} "
                    f"({width:g} x {height:g})."
                )
        return float(width), float(height)

    return float(width if width is not None else 54.0), float(height if height is not None else 36.0)


def create_timing_wrapper(node_func: Callable, component_name: str) -> Callable:
    """Wrap agent node with timing tracking"""
    def wrapper(state: PosterState) -> PosterState:
        start_time = time.time()
        result = node_func(state)
        end_time = time.time()
        elapsed = round(end_time - start_time, 2)

        if component_name == "parser":
            result["timing_metrics"].parser_time = elapsed
        elif component_name == "template_capacity_planner":
            result["timing_metrics"].template_capacity_planner_time = elapsed
        elif component_name == "poster_keypoint_selector":
            result["timing_metrics"].poster_keypoint_selector_time = elapsed
        elif component_name == "curator":
            result["timing_metrics"].curator_time = elapsed
        elif component_name == "template_block_planner":
            result["timing_metrics"].template_block_planner_time = elapsed
        elif component_name == "layout_optimizer":
            result["timing_metrics"].layout_optimizer_time = elapsed
        elif component_name == "color_agent":
            result["timing_metrics"].color_agent_time = elapsed
        elif component_name == "header_planner":
            result["timing_metrics"].header_planner_time = elapsed
        elif component_name == "font_agent":
            result["timing_metrics"].font_agent_time = elapsed
        elif component_name == "micro_layout_refiner":
            result["timing_metrics"].micro_layout_refiner_time = elapsed
        elif component_name == "section_title_designer":
            result["timing_metrics"].title_designer_time = elapsed
        elif component_name == "visual_asset_agent":
            result["timing_metrics"].visual_asset_agent_time = elapsed
        elif component_name == "generated_teaser_agent":
            result["timing_metrics"].generated_teaser_agent_time = elapsed
        elif component_name == "affiliation_logo_agent":
            result["timing_metrics"].affiliation_logo_agent_time = elapsed
        elif component_name == "renderer":
            result["timing_metrics"].renderer_time = elapsed
        elif component_name == "vlm_layout_reviewer":
            result["timing_metrics"].vlm_layout_reviewer_time = elapsed
        elif component_name == "visual_legibility_reviewer":
            result["timing_metrics"].visual_legibility_reviewer_time = elapsed
        elif component_name == "adaptive_column_relayout":
            result["timing_metrics"].adaptive_column_relayout_time = elapsed
        elif component_name == "block_occupancy_analyzer":
            result["timing_metrics"].block_occupancy_analyzer_time = elapsed
        elif component_name == "block_vlm_reviewer":
            result["timing_metrics"].block_vlm_reviewer_time = elapsed
        elif component_name == "block_content_refiner":
            result["timing_metrics"].block_content_refiner_time = elapsed

        return result
    return wrapper


def _block_refinement_max_iterations(state: PosterState | None = None) -> int:
    config = load_config()
    if state and state.get("template_fast_mode"):
        return int(config.get("template_fast_mode", {}).get("emergency_repair_max_iterations", 1))
    return int(config.get("block_refinement", {}).get("max_iterations", 2))


def _route_after_visual_asset_agent(state: PosterState) -> str:
    if state.get("visual_reflow_required") and state.get("visual_reflow_count", 0) <= 1:
        return "layout_optimizer"
    return "renderer"


def _route_after_micro_layout_refiner(state: PosterState) -> str:
    if state.get("draft_status") == "rejected":
        if state.get("template_layout_mode") == "template_prior" and state.get("template_repair_count", 0) < 1:
            return "template_region_relayout"
        return "end"
    return "visual_asset_agent"


def _route_after_renderer(state: PosterState) -> str:
    if state.get("render_stage") == "final" or state.get("final_poster_accepted", False):
        return "end"
    if (
        state.get("enable_block_vlm_review", False)
        and state.get("block_refinement_count", 0) < _block_refinement_max_iterations(state)
        and not state.get("block_vlm_review")
    ):
        return "block_occupancy_analyzer"
    if (
        state.get("enable_visual_legibility_review", False)
        and not state.get("visual_legibility_review")
    ):
        return "visual_legibility_reviewer"
    if state.get("enable_vlm_layout_review", False):
        return "vlm_layout_reviewer"
    return "prepare_final_render"


def _route_after_visual_legibility_reviewer(state: PosterState) -> str:
    if state.get("template_fast_mode"):
        if state.get("enable_vlm_layout_review", False):
            return "vlm_layout_reviewer"
        return "prepare_final_render"
    if state.get("template_repair_required", False):
        return "template_region_relayout"
    if state.get("adaptive_relayout_required", False):
        return "adaptive_column_relayout"
    if state.get("enable_vlm_layout_review", False):
        return "vlm_layout_reviewer"
    return "prepare_final_render"


def _route_after_vlm_layout_reviewer(state: PosterState) -> str:
    if state.get("template_fast_mode") and not state.get("template_repair_required", False):
        return "prepare_final_render"
    if state.get("vlm_reflow_required", False):
        return "visual_asset_agent"
    if state.get("template_repair_required", False):
        return "template_region_relayout"
    return "prepare_final_render"


def _route_after_block_content_refiner(state: PosterState) -> str:
    if state.get("block_refinement_required", False):
        return "layout_optimizer"
    if (
        state.get("enable_visual_legibility_review", False)
        and not state.get("visual_legibility_review")
    ):
        return "visual_legibility_reviewer"
    if state.get("enable_vlm_layout_review", False):
        return "vlm_layout_reviewer"
    return "prepare_final_render"


def _route_after_template_region_relayout(state: PosterState) -> str:
    if state.get("draft_status") == "rejected":
        return "end"
    return "layout_optimizer"


def _prepare_final_render_node(state: PosterState) -> PosterState:
    state["render_stage"] = "final"
    state["vlm_reflow_required"] = False
    state["template_repair_required"] = False
    state["block_refinement_required"] = False
    state["current_agent"] = "prepare_final_render"
    return state


def _load_content_json(state: PosterState, filename: str) -> Dict[str, Any]:
    path = Path(state.get("output_dir", "")) / "content" / filename
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _section_geometry_issues(state: PosterState) -> list[str]:
    layout = state.get("styled_layout") or []
    template = state.get("layout_template_metadata") or {}
    lane_map = {str(lane.get("id")): lane for lane in template.get("lanes") or [] if lane.get("id")}
    containers = [
        element
        for element in layout
        if element.get("type") == "section_container" and element.get("section_id")
    ]
    issues: list[str] = []

    for container in containers:
        lane_id = str(container.get("lane_id") or container.get("slot_id") or "")
        lane = lane_map.get(lane_id)
        if not lane:
            continue
        tolerance = 0.03
        bottom = float(container.get("y", 0.0) or 0.0) + float(container.get("height", 0.0) or 0.0)
        lane_bottom = float(lane.get("y", 0.0) or 0.0) + float(lane.get("h", 0.0) or 0.0)
        if bottom > lane_bottom + tolerance:
            issues.append(f"section exceeds slot {lane_id}: {container.get('section_id')}")

    for index, left in enumerate(containers):
        for right in containers[index + 1:]:
            if _boxes_overlap(left, right):
                issues.append(f"section containers overlap: {left.get('section_id')} and {right.get('section_id')}")
    return issues


def _boxes_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    tolerance = 0.02
    left_x = float(left.get("x", 0.0) or 0.0)
    left_y = float(left.get("y", 0.0) or 0.0)
    left_right = left_x + float(left.get("width", 0.0) or 0.0)
    left_bottom = left_y + float(left.get("height", 0.0) or 0.0)
    right_x = float(right.get("x", 0.0) or 0.0)
    right_y = float(right.get("y", 0.0) or 0.0)
    right_right = right_x + float(right.get("width", 0.0) or 0.0)
    right_bottom = right_y + float(right.get("height", 0.0) or 0.0)
    return not (
        left_right <= right_x + tolerance
        or right_right <= left_x + tolerance
        or left_bottom <= right_y + tolerance
        or right_bottom <= left_y + tolerance
    )


def _run_final_quality_gate(state: PosterState) -> PosterState:
    from src.agents.block_occupancy_analyzer import BlockOccupancyAnalyzer
    from src.utils.visual_footprint import evaluate_visual_footprints

    if state.get("template_layout_mode") != "template_prior":
        state["final_quality_gate"] = {"accepted": True, "reason": "not a template-prior poster"}
        return state

    config = load_config()
    block_settings = config.get("block_refinement", {})
    min_utilization = float(block_settings.get("final_min_utilization", 0.88))
    min_mean_utilization = float(block_settings.get("final_mean_utilization", min_utilization))
    max_bottom_whitespace_inches = float(block_settings.get("final_max_bottom_whitespace_inches", 0.0) or 0.0)
    max_bottom_whitespace_fraction = float(block_settings.get("final_max_bottom_whitespace_fraction", 0.0) or 0.0)
    gate: Dict[str, Any] = {
        "source": "deterministic_final_gate",
        "accepted": True,
        "min_utilization": min_utilization,
        "min_mean_utilization": min_mean_utilization,
        "max_bottom_whitespace_inches": max_bottom_whitespace_inches,
        "max_bottom_whitespace_fraction": max_bottom_whitespace_fraction,
        "failures": [],
    }

    try:
        occupancy_report = BlockOccupancyAnalyzer().analyze(state)
        state["final_block_occupancy_report"] = occupancy_report
        gate["occupancy_summary"] = occupancy_report.get("summary")
        gate["blocks"] = [
            {
                "slot_id": block.get("slot_id"),
                "section_id": block.get("section_id"),
                "section_title": block.get("section_title"),
                "utilization": block.get("utilization"),
                "bottom_whitespace": block.get("bottom_whitespace"),
                "action": block.get("action"),
                "visual_count": block.get("visual_count"),
            }
            for block in occupancy_report.get("blocks", [])
        ]
        low_blocks = [
            {
                "slot_id": block.get("slot_id"),
                "section_id": block.get("section_id"),
                "section_title": block.get("section_title"),
                "utilization": block.get("utilization"),
            }
            for block in occupancy_report.get("blocks", [])
            if float(block.get("utilization") or 0.0) < min_utilization
        ]
        whitespace_blocks = []
        for block in occupancy_report.get("blocks", []):
            available_height = float(block.get("available_height") or 0.0)
            bottom_whitespace = float(block.get("bottom_whitespace") or 0.0)
            allowed_values = [
                value
                for value in (
                    max_bottom_whitespace_inches,
                    available_height * max_bottom_whitespace_fraction if max_bottom_whitespace_fraction > 0 else 0.0,
                )
                if value > 0
            ]
            if not allowed_values:
                continue
            allowed_whitespace = min(allowed_values)
            if bottom_whitespace > allowed_whitespace + 1e-6:
                whitespace_blocks.append(
                    {
                        "slot_id": block.get("slot_id"),
                        "section_id": block.get("section_id"),
                        "section_title": block.get("section_title"),
                        "bottom_whitespace": block.get("bottom_whitespace"),
                        "allowed": round(allowed_whitespace, 4),
                    }
                )
        if not occupancy_report.get("blocks"):
            gate["failures"].append({"category": "occupancy", "reason": "no content blocks measured"})
        if low_blocks:
            gate["failures"].append({"category": "occupancy", "low_blocks": low_blocks})
        if whitespace_blocks:
            gate["failures"].append({"category": "bottom_whitespace", "blocks": whitespace_blocks})
        mean_utilization = float((occupancy_report.get("summary") or {}).get("mean_utilization") or 0.0)
        if occupancy_report.get("blocks") and mean_utilization < min_mean_utilization:
            gate["failures"].append(
                {
                    "category": "occupancy_mean",
                    "mean_utilization": mean_utilization,
                    "required": min_mean_utilization,
                }
            )
    except Exception as exc:
        gate["failures"].append({"category": "occupancy", "reason": str(exc)})

    try:
        visual_footprint = evaluate_visual_footprints(
            state.get("styled_layout") or [],
            state.get("layout_template_metadata") or {},
            state,
            config,
        )
        gate["visual_footprint"] = visual_footprint
        if visual_footprint.get("violations"):
            gate["failures"].append(
                {
                    "category": "visual_footprint",
                    "violations": visual_footprint["violations"],
                }
            )
    except Exception as exc:
        gate["failures"].append({"category": "visual_footprint", "reason": str(exc)})

    micro_report = _load_content_json(state, "micro_layout_report.json")
    micro_issues = ((micro_report.get("validation") or {}).get("issues") or [])
    if micro_issues:
        gate["failures"].append({"category": "micro_layout", "issues": micro_issues})

    geometry_issues = _section_geometry_issues(state)
    if geometry_issues:
        gate["failures"].append({"category": "section_geometry", "issues": geometry_issues})

    vlm_review = state.get("vlm_layout_review") or {}
    high_issues = [
        issue
        for issue in (vlm_review.get("issues") or [])
        if str(issue.get("severity", "")).lower() == "high"
        and str(issue.get("category", "")).lower() in {"whitespace", "overflow"}
    ]
    global_assessment = vlm_review.get("global_assessment") or {}
    high_whitespace_regions = [
        region
        for region in (global_assessment.get("major_whitespace_regions") or [])
        if str(region.get("severity", "")).lower() == "high"
    ]
    title_readability = str(global_assessment.get("title_readability") or "ok").lower()
    if high_issues:
        gate["failures"].append({"category": "vlm_high_issue", "issues": high_issues})
    if high_whitespace_regions:
        gate["failures"].append({"category": "vlm_major_whitespace", "regions": high_whitespace_regions})
    if title_readability in {"too_small", "crowded", "unclear"}:
        gate["failures"].append({"category": "title_readability", "status": title_readability})

    gate["accepted"] = not gate["failures"]
    state["final_quality_gate"] = gate

    output_dir = Path(state["output_dir"]) / "content"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "final_quality_gate.json", "w", encoding="utf-8") as f:
        json.dump(gate, f, indent=2)
    if state.get("final_block_occupancy_report"):
        with open(output_dir / "final_block_occupancy_report.json", "w", encoding="utf-8") as f:
            json.dump(state["final_block_occupancy_report"], f, indent=2)

    if not gate["accepted"]:
        state["final_poster_accepted"] = False
        state.setdefault("errors", []).append(f"final_quality_gate: {gate['failures']}")
        log_agent_error("final_quality_gate", f"rejected final poster: {gate['failures']}")
    else:
        log_agent_success("final_quality_gate", "accepted final poster")
    return state


def create_workflow_graph():
    """create the langgraph workflow"""
    from langgraph.graph import StateGraph, START, END
    from src.agents.adaptive_column_relayout import adaptive_column_relayout_node
    from src.agents.background_image_agent import background_image_agent_node
    from src.agents.block_content_refiner import block_content_refiner_node
    from src.agents.block_occupancy_analyzer import block_occupancy_analyzer_node
    from src.agents.block_vlm_reviewer import block_vlm_reviewer_node
    from src.agents.color_agent import color_agent_node
    from src.agents.affiliation_logo_agent import affiliation_logo_agent_node
    from src.agents.curator import curator_node
    from src.agents.font_agent import font_agent_node
    from src.agents.generated_teaser_agent import generated_teaser_agent_node
    from src.agents.header_planner import header_planner_node
    from src.agents.layout_with_balancer import layout_with_balancer_node as layout_optimizer_node
    from src.agents.micro_layout_refiner import micro_layout_refiner_node
    from src.agents.parser import parser_node
    from src.agents.poster_keypoint_selector import poster_keypoint_selector_node
    from src.agents.renderer import renderer_node
    from src.agents.section_title_designer import section_title_designer_node
    from src.agents.standard_template_preselector import standard_template_preselector_node
    from src.agents.template_capacity_planner import template_capacity_planner_node
    from src.agents.template_block_planner import template_block_planner_node
    from src.agents.template_region_relayout import template_region_relayout_node
    from src.agents.vlm_layout_reviewer import vlm_layout_reviewer_node
    from src.agents.visual_asset_agent import visual_asset_agent_node
    from src.agents.visual_legibility_reviewer import visual_legibility_reviewer_node

    graph = StateGraph(PosterState)

    graph.add_node("parser", create_timing_wrapper(parser_node, "parser"))
    graph.add_node("affiliation_logo_agent", create_timing_wrapper(affiliation_logo_agent_node, "affiliation_logo_agent"))
    graph.add_node("standard_template_preselector", create_timing_wrapper(standard_template_preselector_node, "standard_template_preselector"))
    graph.add_node("template_capacity_planner", create_timing_wrapper(template_capacity_planner_node, "template_capacity_planner"))
    graph.add_node("poster_keypoint_selector", create_timing_wrapper(poster_keypoint_selector_node, "poster_keypoint_selector"))
    graph.add_node("curator", create_timing_wrapper(curator_node, "curator"))
    graph.add_node("generated_teaser_agent", create_timing_wrapper(generated_teaser_agent_node, "generated_teaser_agent"))
    graph.add_node("template_block_planner", create_timing_wrapper(template_block_planner_node, "template_block_planner"))
    graph.add_node("color_agent", create_timing_wrapper(color_agent_node, "color_agent"))
    graph.add_node("header_planner", create_timing_wrapper(header_planner_node, "header_planner"))
    graph.add_node("section_title_designer", create_timing_wrapper(section_title_designer_node, "section_title_designer"))
    graph.add_node("layout_optimizer", create_timing_wrapper(layout_optimizer_node, "layout_optimizer"))
    graph.add_node("font_agent", create_timing_wrapper(font_agent_node, "font_agent"))
    graph.add_node("micro_layout_refiner", create_timing_wrapper(micro_layout_refiner_node, "micro_layout_refiner"))
    graph.add_node("visual_asset_agent", create_timing_wrapper(visual_asset_agent_node, "visual_asset_agent"))
    graph.add_node("renderer", create_timing_wrapper(renderer_node, "renderer"))
    graph.add_node("block_occupancy_analyzer", create_timing_wrapper(block_occupancy_analyzer_node, "block_occupancy_analyzer"))
    graph.add_node("block_vlm_reviewer", create_timing_wrapper(block_vlm_reviewer_node, "block_vlm_reviewer"))
    graph.add_node("block_content_refiner", create_timing_wrapper(block_content_refiner_node, "block_content_refiner"))
    graph.add_node("visual_legibility_reviewer", create_timing_wrapper(visual_legibility_reviewer_node, "visual_legibility_reviewer"))
    graph.add_node("adaptive_column_relayout", create_timing_wrapper(adaptive_column_relayout_node, "adaptive_column_relayout"))
    graph.add_node("template_region_relayout", template_region_relayout_node)
    graph.add_node("vlm_layout_reviewer", create_timing_wrapper(vlm_layout_reviewer_node, "vlm_layout_reviewer"))
    graph.add_node("prepare_final_render", _prepare_final_render_node)
    graph.add_node("background_image_agent", create_timing_wrapper(background_image_agent_node, "background_image_agent"))

    graph.add_edge(START, "parser")
    graph.add_edge("parser", "affiliation_logo_agent")
    graph.add_edge("affiliation_logo_agent", "standard_template_preselector")
    graph.add_edge("standard_template_preselector", "template_capacity_planner")
    graph.add_edge("template_capacity_planner", "poster_keypoint_selector")
    graph.add_edge("poster_keypoint_selector", "curator")
    graph.add_edge("curator", "color_agent")
    graph.add_edge("color_agent", "header_planner")
    graph.add_edge("header_planner", "generated_teaser_agent")
    graph.add_edge("generated_teaser_agent", "template_block_planner")
    graph.add_edge("template_block_planner", "section_title_designer")
    graph.add_edge("section_title_designer", "layout_optimizer")
    graph.add_edge("layout_optimizer", "font_agent")
    graph.add_edge("font_agent", "micro_layout_refiner")
    graph.add_conditional_edges(
        "micro_layout_refiner",
        _route_after_micro_layout_refiner,
        {
            "visual_asset_agent": "visual_asset_agent",
            "template_region_relayout": "template_region_relayout",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "visual_asset_agent",
        _route_after_visual_asset_agent,
        {
            "layout_optimizer": "layout_optimizer",
            "renderer": "renderer",
        },
    )
    graph.add_conditional_edges(
        "renderer",
        _route_after_renderer,
        {
            "block_occupancy_analyzer": "block_occupancy_analyzer",
            "visual_legibility_reviewer": "visual_legibility_reviewer",
            "vlm_layout_reviewer": "vlm_layout_reviewer",
            "prepare_final_render": "prepare_final_render",
            "end": END,
        },
    )
    graph.add_edge("block_occupancy_analyzer", "block_vlm_reviewer")
    graph.add_edge("block_vlm_reviewer", "block_content_refiner")
    graph.add_conditional_edges(
        "block_content_refiner",
        _route_after_block_content_refiner,
        {
            "layout_optimizer": "layout_optimizer",
            "visual_legibility_reviewer": "visual_legibility_reviewer",
            "vlm_layout_reviewer": "vlm_layout_reviewer",
            "prepare_final_render": "prepare_final_render",
        },
    )
    graph.add_conditional_edges(
        "visual_legibility_reviewer",
        _route_after_visual_legibility_reviewer,
        {
            "template_region_relayout": "template_region_relayout",
            "adaptive_column_relayout": "adaptive_column_relayout",
            "vlm_layout_reviewer": "vlm_layout_reviewer",
            "prepare_final_render": "prepare_final_render",
        },
    )
    graph.add_edge("adaptive_column_relayout", "layout_optimizer")
    graph.add_conditional_edges(
        "template_region_relayout",
        _route_after_template_region_relayout,
        {
            "layout_optimizer": "layout_optimizer",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "vlm_layout_reviewer",
        _route_after_vlm_layout_reviewer,
        {
            "visual_asset_agent": "visual_asset_agent",
            "template_region_relayout": "template_region_relayout",
            "prepare_final_render": "prepare_final_render",
        },
    )
    graph.add_edge("prepare_final_render", "background_image_agent")
    graph.add_edge("background_image_agent", "renderer")

    return graph


def save_timing_log(state: PosterState):
    """Save timing and cost metrics to log file"""
    output_dir = Path(state["output_dir"])
    log_path = output_dir / "timing_cost_log.json"

    metrics = state["timing_metrics"]
    total_time = metrics.get_total_time()

    block_settings = (
        (state.get("block_occupancy_report") or {}).get("settings")
        or load_config().get("block_refinement", {})
    )

    api_calls_by_agent = {}
    total_input_tokens = 0
    total_output_tokens = 0

    for call in metrics.api_calls:
        if call.agent not in api_calls_by_agent:
            api_calls_by_agent[call.agent] = {
                "count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": []
            }
        api_calls_by_agent[call.agent]["count"] += 1
        api_calls_by_agent[call.agent]["input_tokens"] += call.input_tokens
        api_calls_by_agent[call.agent]["output_tokens"] += call.output_tokens
        api_calls_by_agent[call.agent]["calls"].append({
            "type": call.call_type,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "timestamp": call.timestamp
        })
        total_input_tokens += call.input_tokens
        total_output_tokens += call.output_tokens

    log_data = {
        "overall": {
            "total_runtime_seconds": total_time,
            "total_runtime_minutes": round(total_time / 60, 2),
            "total_api_calls": metrics.get_api_call_count(),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens
        },
        "component_timing": {
            "parser": {
                "time_seconds": round(metrics.parser_time, 2),
                "percentage": metrics.get_component_percentage(metrics.parser_time)
            },
            "template_capacity_planner": {
                "time_seconds": round(metrics.template_capacity_planner_time, 2),
                "percentage": metrics.get_component_percentage(metrics.template_capacity_planner_time)
            },
            "poster_keypoint_selector": {
                "time_seconds": round(metrics.poster_keypoint_selector_time, 2),
                "percentage": metrics.get_component_percentage(metrics.poster_keypoint_selector_time)
            },
            "curator": {
                "time_seconds": round(metrics.curator_time, 2),
                "percentage": metrics.get_component_percentage(metrics.curator_time)
            },
            "template_block_planner": {
                "time_seconds": round(metrics.template_block_planner_time, 2),
                "percentage": metrics.get_component_percentage(metrics.template_block_planner_time)
            },
            "layout_optimizer": {
                "time_seconds": round(metrics.layout_optimizer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.layout_optimizer_time)
            },
            "color_agent": {
                "time_seconds": round(metrics.color_agent_time, 2),
                "percentage": metrics.get_component_percentage(metrics.color_agent_time)
            },
            "header_planner": {
                "time_seconds": round(metrics.header_planner_time, 2),
                "percentage": metrics.get_component_percentage(metrics.header_planner_time)
            },
            "font_agent": {
                "time_seconds": round(metrics.font_agent_time, 2),
                "percentage": metrics.get_component_percentage(metrics.font_agent_time)
            },
            "micro_layout_refiner": {
                "time_seconds": round(metrics.micro_layout_refiner_time, 2),
                "percentage": metrics.get_component_percentage(metrics.micro_layout_refiner_time)
            },
            "title_designer": {
                "time_seconds": round(metrics.title_designer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.title_designer_time)
            },
            "visual_asset_agent": {
                "time_seconds": round(metrics.visual_asset_agent_time, 2),
                "percentage": metrics.get_component_percentage(metrics.visual_asset_agent_time)
            },
            "generated_teaser_agent": {
                "time_seconds": round(metrics.generated_teaser_agent_time, 2),
                "percentage": metrics.get_component_percentage(metrics.generated_teaser_agent_time)
            },
            "affiliation_logo_agent": {
                "time_seconds": round(metrics.affiliation_logo_agent_time, 2),
                "percentage": metrics.get_component_percentage(metrics.affiliation_logo_agent_time)
            },
            "renderer": {
                "time_seconds": round(metrics.renderer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.renderer_time)
            },
            "vlm_layout_reviewer": {
                "time_seconds": round(metrics.vlm_layout_reviewer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.vlm_layout_reviewer_time)
            },
            "visual_legibility_reviewer": {
                "time_seconds": round(metrics.visual_legibility_reviewer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.visual_legibility_reviewer_time)
            },
            "adaptive_column_relayout": {
                "time_seconds": round(metrics.adaptive_column_relayout_time, 2),
                "percentage": metrics.get_component_percentage(metrics.adaptive_column_relayout_time)
            },
            "block_occupancy_analyzer": {
                "time_seconds": round(metrics.block_occupancy_analyzer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.block_occupancy_analyzer_time)
            },
            "block_vlm_reviewer": {
                "time_seconds": round(metrics.block_vlm_reviewer_time, 2),
                "percentage": metrics.get_component_percentage(metrics.block_vlm_reviewer_time)
            },
            "block_content_refiner": {
                "time_seconds": round(metrics.block_content_refiner_time, 2),
                "percentage": metrics.get_component_percentage(metrics.block_content_refiner_time)
            }
        },
        "api_calls_by_agent": api_calls_by_agent,
        "model_info": {
            "text_model": f"{state['text_model'].provider}/{state['text_model'].model_name}",
            "vision_model": f"{state['vision_model'].provider}/{state['vision_model'].model_name}",
            "vlm_layout_review": {
                "enabled": state.get("enable_vlm_layout_review", False),
                "model": state.get("vlm_model") or os.getenv("VLM_MODEL"),
            },
            "visual_legibility_review": {
                "enabled": state.get("enable_visual_legibility_review", False),
                "adaptive_column_width": state.get("enable_adaptive_column_width", False),
                "adaptive_relayout_count": state.get("adaptive_relayout_count", 0),
                "adaptive_lane_widths": state.get("adaptive_lane_widths"),
            },
            "block_refinement": {
                "enabled": state.get("enable_block_vlm_review", False),
                "block_refinement_count": state.get("block_refinement_count", 0),
                "target_utilization": block_settings.get("target_utilization", 0.95),
            },
            "header_plan": {
                "route": (state.get("header_plan") or {}).get("route"),
                "subtitle": bool(((state.get("header_plan") or {}).get("subtitle") or {}).get("text")),
                "fallback": (state.get("header_plan") or {}).get("fallback", False),
                "validation": (state.get("header_plan") or {}).get("validation"),
            },
            "generated_teaser": {
                "enabled": state.get("enable_generated_teaser", False),
                "target_section_id": (state.get("generated_teaser_report") or {}).get("target_section_id"),
                "asset_id": (state.get("generated_teaser_report") or {}).get("asset_id"),
            },
        }
    }

    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, indent=2)

    log_agent_success("pipeline", f"Timing log saved to: {log_path}")
    return log_data


def main():
    config = load_config()
    default_text_model = os.getenv("PAPER2POSTER_TEXT_MODEL") or os.getenv("PAPER2POSTER_MODEL") or "gpt-5.4"
    default_vision_model = (
        os.getenv("PAPER2POSTER_VISION_MODEL")
        or os.getenv("PAPER2POSTER_MODEL")
        or os.getenv("VLM_MODEL")
        or default_text_model
    )
    default_vlm_model = os.getenv("PAPER2POSTER_VLM_MODEL") or os.getenv("VLM_MODEL")
    poster_style_choices = available_poster_styles(config)
    visual_density_choices = available_visual_densities(config)
    default_poster_style = normalize_poster_style(os.getenv("PAPER2POSTER_STYLE"), config)
    default_visual_density = normalize_visual_density(os.getenv("PAPER2POSTER_VISUAL_DENSITY"), config)
    header_route_choices = ["auto", "classic_left", "centered", "right_title", "split_logos"]
    header_subtitle_choices = ["auto", "off", "always"]
    default_header_route = os.getenv("PAPER2POSTER_HEADER_ROUTE", "auto")
    if default_header_route not in header_route_choices:
        default_header_route = "auto"
    default_header_subtitle = os.getenv("PAPER2POSTER_HEADER_SUBTITLE", "auto")
    if default_header_subtitle not in header_subtitle_choices:
        default_header_subtitle = "auto"
    header_seed_env = os.getenv("PAPER2POSTER_HEADER_SEED")
    try:
        default_header_seed = int(header_seed_env) if header_seed_env else None
    except ValueError:
        default_header_seed = None

    parser = argparse.ArgumentParser(description="Paper2Poster: Multi-agent Aesthetic-aware Paper-to-poster generation")
    parser.add_argument("paper_path_positional", nargs="?", help="Path to the PDF paper")
    parser.add_argument("--paper_path", type=str, required=False, help="Path to the PDF paper")
    parser.add_argument("--text_model", type=str, default=default_text_model,
                       choices=["gpt-5", "gpt-5.1", "gpt-5.4", "gpt-5.4-xhigh", "gpt-5.5-xhigh", "gpt-4o-2024-08-06", "gpt-4.1-2025-04-14", "gpt-4.1-mini-2025-04-14", "claude-sonnet-4-20250514", "claude-opus-4.5", "gemini-2.5-pro", "glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4", "kimi-k2-turbo-preview", "MiniMax-M2", "qwen3-max"],
                       help="Text model for content processing")
    parser.add_argument("--vision_model", type=str, default=default_vision_model,
                       choices=["gpt-5", "gpt-5.1", "gpt-5.4", "gpt-5.4-xhigh", "gpt-5.5-xhigh", "gpt-4o-2024-08-06", "gpt-4.1-2025-04-14", "gpt-4.1-mini-2025-04-14", "claude-sonnet-4-20250514", "claude-opus-4.5", "gemini-2.5-pro", "glm-4.6v", "glm-4.5v", "glm-4v", "moonshot-v1-8k-vision-preview", "MiniMax-M2", "qwen3-vl-plus"],
                       help="Vision model for image analysis")
    parser.add_argument("--poster_width", type=float, default=None, help="Poster width in inches")
    parser.add_argument("--poster_height", type=float, default=None, help="Poster height in inches")
    parser.add_argument(
        "--layout-template",
        type=str,
        default="three_column_postergen",
        choices=LayoutTemplates.all_cli_template_choices(),
        help="Layout template family to use for poster composition.",
    )
    parser.add_argument(
        "--list-layout-templates",
        action="store_true",
        help="List built-in and extracted layout templates, then exit.",
    )
    parser.add_argument("--url", type=str, help="URL for QR code on poster") # TODO
    parser.add_argument("--logo", type=str, default="", help="Path to conference/journal logo (overrides --conference)")
    parser.add_argument("--conference", type=str, default="", help="Conference name, e.g. 'CVPR', 'NeurIPS 2025'. Auto-resolved to local logo.")
    parser.add_argument("--aff_logo", "--aff-logo", dest="aff_logo", type=str, default="", help="Path to affiliation logo")
    parser.add_argument(
        "--enable-visual-refinement",
        action="store_true",
        help="Enable the second-phase visual asset agent for edit/generate planning.",
    )
    parser.add_argument(
        "--enable-affiliation-logos",
        action="store_true",
        help="Extract author affiliations and place institution logos in the title area.",
    )
    parser.add_argument(
        "--enable-vlm-layout-review",
        action="store_true",
        help="Enable VLM screenshot review and one-pass safe layout correction.",
    )
    parser.add_argument(
        "--enable-visual-legibility-review",
        action="store_true",
        help="Enable VLM/heuristic review for tiny text inside figures and tables.",
    )
    parser.add_argument(
        "--enable-block-vlm-review",
        action="store_true",
        help="Enable block-level VLM review plus 95%% utilization content refinement.",
    )
    parser.add_argument(
        "--enable-adaptive-column-width",
        action="store_true",
        help="Allow one adaptive three-column width relayout when visual text is too small.",
    )
    parser.add_argument(
        "--enable-generated-background",
        action="store_true",
        help="Generate a low-contrast academic background image and place it behind the poster.",
    )
    parser.add_argument(
        "--enable-generated-teaser",
        action="store_true",
        help="Generate a paper-specific conceptual teaser visual for the motivation/introduction block.",
    )
    parser.add_argument(
        "--background-palette",
        choices=["light_blue", "light_gray"],
        default=None,
        help="Palette for the generated poster background when --enable-generated-background is set.",
    )
    parser.add_argument(
        "--poster-style",
        choices=poster_style_choices,
        default=default_poster_style,
        help="Typography and color preset for poster title bars, panels, and highlights.",
    )
    parser.add_argument(
        "--visual-density",
        choices=visual_density_choices,
        default=default_visual_density,
        help="How aggressively the planner should preserve figures and result tables.",
    )
    parser.add_argument(
        "--header-route",
        choices=header_route_choices,
        default=default_header_route,
        help="Header composition route for title, authors, and logos.",
    )
    parser.add_argument(
        "--header-subtitle",
        choices=header_subtitle_choices,
        default=default_header_subtitle,
        help="Whether to add a short generated subtitle when the paper title is short enough.",
    )
    parser.add_argument(
        "--header-seed",
        type=int,
        default=default_header_seed,
        help="Optional seed for reproducible auto header route and subtitle choices.",
    )
    parser.add_argument(
        "--vlm-model",
        type=str,
        default=default_vlm_model,
        help="VLM model name for layout review. Falls back to VLM_MODEL env var.",
    )
    
    args = parser.parse_args()

    if args.list_layout_templates:
        print("Available layout templates:")
        for template_name in LayoutTemplates.all_cli_template_choices():
            if is_block_template_id(template_name):
                info = get_block_template_info(template_name) or {}
                size = info.get("recommended_canvas_size") or {}
                print(
                    f"- {template_name} "
                    f"({info.get('orientation', 'unknown')}, "
                    f"{info.get('slot_count', '?')} slots, "
                    f"{size.get('width', '?')}x{size.get('height', '?')} in)"
                )
            else:
                print(f"- {template_name}")
        return 0
    
    try:
        final_width, final_height = resolve_poster_dimensions(
            args.layout_template,
            args.poster_width,
            args.poster_height,
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    # poster dimensions: use the requested/inferred canvas directly so portrait templates work.
    input_ratio = final_width / final_height
    normalized_ratio = max(input_ratio, 1 / input_ratio)
    if normalized_ratio > 2.1:
        print(f"❌ Poster ratio is out of range: {input_ratio:.3f}. Please use a landscape or portrait ratio up to 2.1:1.")
        return 1
    
    is_cluster_template = is_block_template_id(args.layout_template)
    if is_cluster_template:
        args.enable_visual_legibility_review = True
        args.enable_vlm_layout_review = True
        args.enable_block_vlm_review = True

    # check .env file
    if env_path.exists():
        print(f"✅ .env file found at: {env_path}")
    else:
        print(f"❌ .env file NOT found")
    
    # check api keys
    required_keys = {"openai": "OPENAI_API_KEY", "openai_responses": "VLM_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY", "zhipu": "ZHIPU_API_KEY", "moonshot": "MOONSHOT_API_KEY", "Minimax": "MINIMAX_API_KEY", "Alibaba": "ALIBABA_API_KEY"}
    model_providers = {"claude-sonnet-4-20250514": "anthropic", "claude-opus-4.5": "anthropic", "claude-opus-4-5-20251101": "anthropic", "gemini": "google", "gemini-2.5-pro": "google",
                      "gpt-5.1": "openai", "gpt-5.4-xhigh": "openai", "gpt-5.5-xhigh": "openai", "gpt-4o-2024-08-06": "openai", "gpt-4.1-2025-04-14": "openai", "gpt-4.1-mini-2025-04-14": "openai",
                      "gpt-5.4": "openai",
                      "glm-4.6": "zhipu", "glm-4.6v": "zhipu", "glm-4.5": "zhipu", "glm-4.5-air": "zhipu", "glm-4.5v": "zhipu", "glm-4": "zhipu", "glm-4v": "zhipu",
                      "kimi-k2-turbo-preview": "moonshot", "moonshot-v1-8k-vision-preview": "moonshot",
                      "qwen3-max": "Alibaba", "qwen3-vl-plus": "Alibaba",
                      "MiniMax-M2":"Minimax",}
    
    needed_keys = set()
    if args.text_model in model_providers:
        needed_keys.add(required_keys[model_providers[args.text_model]])
    if args.vision_model in model_providers:
        needed_keys.add(required_keys[model_providers[args.vision_model]])
    if (
        args.enable_vlm_layout_review
        or args.enable_visual_legibility_review
        or args.enable_block_vlm_review
    ) and not os.getenv("VLM_API_KEY"):
        needed_keys.add("VLM_API_KEY")
    
    missing = [k for k in needed_keys if not os.getenv(k)]
    if missing:
        print(f"❌ Missing API keys: {missing}")
        return 1
    
    # get pdf path
    pdf_path = args.paper_path or args.paper_path_positional
    if not pdf_path or not Path(pdf_path).exists():
        print("❌ PDF not found")
        return 1
    
    print(f"🚀 Paper2Poster Pipeline")
    print(f"📄 PDF: {pdf_path}")
    print(f"🤖 Models: {args.text_model}/{args.vision_model}")
    print(f"📏 Size: {final_width}\" × {final_height:.2f}\"")
    print(f"🧩 Layout Template: {args.layout_template}")

    # resolve conference logo: explicit --logo beats --conference auto-resolve
    resolved_logo_path = args.logo if args.logo and Path(args.logo).exists() else ""
    conference_name = args.conference or ""
    if not resolved_logo_path and conference_name:
        from src.utils.conference_logos import resolve_conference_logo
        resolved_logo_path = resolve_conference_logo(conference_name) or ""
        if resolved_logo_path:
            print(f"🏛️  Conference Logo: {resolved_logo_path} (auto: {conference_name})")
        else:
            print(f"⚠️  Conference '{conference_name}' not in local library — no logo")
    elif resolved_logo_path:
        print(f"🏛️  Conference Logo: {resolved_logo_path}")

    print(f"🏫 Affiliation Logo: {args.aff_logo}")
    print(f"🎓 Auto Affiliation Logos: {'enabled' if args.enable_affiliation_logos else 'disabled'}")
    print(f"👁️ VLM Layout Review: {'enabled' if args.enable_vlm_layout_review else 'disabled'}")
    print(f"🔎 Visual Legibility Review: {'enabled' if args.enable_visual_legibility_review else 'disabled'}")
    print(f"🧱 Block 95% Refinement: {'enabled' if args.enable_block_vlm_review else 'disabled'}")
    print(f"📐 Adaptive Column Width: {'enabled' if args.enable_adaptive_column_width else 'disabled'}")
    print(f"🎭 Poster Style: {args.poster_style}")
    print(f"📊 Visual Density: {args.visual_density}")
    print(f"🧾 Header Route: {args.header_route}")
    print(f"🧾 Header Subtitle: {args.header_subtitle}")
    if args.header_seed is not None:
        print(f"🧾 Header Seed: {args.header_seed}")
    print(f"🖼️ Generated Teaser: {'enabled' if args.enable_generated_teaser else 'disabled'}")
    print(f"🎨 Generated Background: {'enabled' if args.enable_generated_background else 'disabled'}")
    if args.enable_generated_background:
        print(f"🎨 Background Palette: {args.background_palette or 'config default'}")
    
    try:
        state = create_state(
            pdf_path, args.text_model, args.vision_model,
            final_width, final_height,
            args.layout_template,
            args.url, resolved_logo_path, args.aff_logo,
            enable_visual_refinement=args.enable_visual_refinement,
            enable_affiliation_logos=args.enable_affiliation_logos,
            enable_vlm_layout_review=args.enable_vlm_layout_review,
            enable_visual_legibility_review=args.enable_visual_legibility_review,
            enable_block_vlm_review=args.enable_block_vlm_review,
            enable_adaptive_column_width=args.enable_adaptive_column_width,
            enable_generated_background=args.enable_generated_background,
            enable_generated_teaser=args.enable_generated_teaser,
            background_palette=args.background_palette,
            poster_style_preset=args.poster_style,
            visual_density=args.visual_density,
            header_route=args.header_route,
            header_subtitle_policy=args.header_subtitle,
            header_seed=args.header_seed,
            vlm_model=args.vlm_model,
            conference_name=conference_name,
        )

        state["timing_metrics"].pipeline_start = time.time()

        log_agent_info("pipeline", "creating workflow graph")
        graph = create_workflow_graph()
        workflow = graph.compile()

        log_agent_info("pipeline", "executing workflow")
        final_state = workflow.invoke(state, config={"recursion_limit": 64})

        final_state["timing_metrics"].pipeline_end = time.time()

        if not final_state.get("errors") and final_state.get("final_poster_accepted", False):
            final_state = _run_final_quality_gate(final_state)

        if final_state.get("errors"):
            log_agent_error("pipeline", f"Pipeline errors: {final_state['errors']}")
            return 1
        if not final_state.get("final_poster_accepted", False):
            log_agent_error("pipeline", f"Poster rejected before final acceptance. Draft status: {final_state.get('draft_status')}")
            return 1
        required_outputs = ["story_board", "design_layout", "color_scheme", "styled_layout", "resolved_visual_assets"]

        def is_missing_required_output(output_name: str) -> bool:
            value = final_state.get(output_name)
            if output_name == "resolved_visual_assets":
                return value is None
            return not value

        missing = [out for out in required_outputs if is_missing_required_output(out)]
        if missing:
            log_agent_error("pipeline", f"Missing outputs: {missing}")
            return 1
        
        log_agent_success("pipeline", "Pipeline completed successfully")

        # full pipeline summary
        log_agent_success("pipeline", "Full pipeline complete")

        timing_log = save_timing_log(final_state)
        total_time = timing_log["overall"]["total_runtime_seconds"]
        total_calls = timing_log["overall"]["total_api_calls"]

        log_agent_info("pipeline", f"Total runtime: {total_time}s ({total_time/60:.2f} minutes)")
        log_agent_info("pipeline", f"Total API calls: {total_calls}")
        log_agent_info("pipeline", f"Total tokens: {final_state['tokens'].input_text} → {final_state['tokens'].output_text}")

        output_path = Path(final_state["output_dir"]) / f"{final_state['poster_name']}.pptx"
        log_agent_info("pipeline", f"Final poster saved to: {output_path}")

        return 0
        
    except Exception as e:
        log_agent_error("pipeline", f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
