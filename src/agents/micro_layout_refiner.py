"""
Deterministic post-font layout refinement.

This stage keeps the semantic three-lane reading flow intact while tightening
section geometry, spacing, and font sizes to avoid overlap and overflow before
visual assets are resolved and rendered.
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config.poster_config import load_config
from src.layout.text_height_measurement import measure_text_height
from src.state.poster_state import PosterState
from src.tools.layout_api import LayoutTemplates
from src.utils.visual_footprint import enforce_visual_footprint, visual_footprint_config
from utils.src.logging_utils import log_agent_error, log_agent_info, log_agent_success, log_agent_warning


class MicroLayoutRefiner:
    def __init__(self):
        self.name = "micro_layout_refiner"
        self.config = load_config()
        self.refine_config = self.config["micro_layout_refinement"]
        self.layout_config = self.config["layout"]
        self.typography_config = self.config["typography"]

    def __call__(self, state: PosterState) -> PosterState:
        log_agent_info(self.name, "refining styled layout for final fit")
        state["draft_status"] = "pending"
        state["final_poster_accepted"] = False

        try:
            styled_layout = state.get("styled_layout") or []
            if not styled_layout:
                raise ValueError("missing styled_layout from font agent")

            template_layout = state.get("layout_template_metadata") or self._resolve_template_layout(state)
            refined_layout, report = self._refine_layout(styled_layout, template_layout, state)

            state["styled_layout"] = refined_layout
            state["current_agent"] = self.name
            self._save_outputs(state, report)

            if report["validation"]["issues"]:
                state["draft_status"] = "rejected"
                state["draft_rejection_reason"] = (
                    "layout refinement failed validation: "
                    + "; ".join(report["validation"]["issues"])
                )
                log_agent_warning(self.name, state["draft_rejection_reason"])
                return state
            state["draft_status"] = "accepted"
            if report["force_fit_used"]:
                log_agent_warning(self.name, "force-fit fallback used on at least one lane")
            log_agent_success(self.name, f"refined layout with {report['validated_elements']} positioned elements")
        except Exception as e:
            log_agent_error(self.name, f"failed: {e}")
            state["draft_status"] = "rejected"
            state["draft_rejection_reason"] = str(e)
            state["errors"].append(f"{self.name}: {e}")

        return state

    def _resolve_template_layout(self, state: PosterState) -> Dict[str, Any]:
        poster_width = state["poster_width"]
        poster_height = state["poster_height"]
        poster_margin = self.layout_config["poster_margin"]
        column_spacing = self.layout_config["column_spacing"]
        title_height_fraction = self.layout_config["title_height_fraction"]
        effective_height = poster_height - 2 * poster_margin
        title_region_height = effective_height * title_height_fraction

        requested_template = state.get("resolved_layout_template") or state.get("layout_template", "three_column_postergen")
        return LayoutTemplates(
            poster_width,
            poster_height,
            margin=poster_margin,
            col_gap=column_spacing,
        ).get_template(requested_template, header_height=title_region_height)

    def _refine_layout(self, styled_layout: List[Dict[str, Any]], template_layout: Dict[str, Any], state: PosterState) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        lane_map = {lane["id"]: lane for lane in template_layout["lanes"]}
        title_and_global = []
        section_containers = []
        section_groups: Dict[str, Dict[str, Any]] = {}

        for element in styled_layout:
            if element.get("type") == "section_container":
                lane_id = element.get("lane_id") or self._match_lane_for_element(element, lane_map)
                group = {
                    "section_id": element["section_id"],
                    "lane_id": lane_id,
                    "container": deepcopy(element),
                    "children": [],
                }
                section_containers.append(group)
                section_groups[element["section_id"]] = group
            elif element.get("type") in {
                "title",
                "conf_logo",
                "aff_logo",
                "institution_logo",
                "logo_divider",
                "qr_code",
                "template_background",
                "template_header_background",
                "template_footer_background",
            }:
                title_and_global.append(deepcopy(element))

        for element in styled_layout:
            if element.get("type") == "section_container" or element.get("type") in {
                "title",
                "conf_logo",
                "aff_logo",
                "institution_logo",
                "logo_divider",
                "qr_code",
                "template_background",
                "template_header_background",
                "template_footer_background",
            }:
                continue
            section_id = self._assign_section_id(element, section_containers, lane_map)
            if section_id and section_id in section_groups:
                section_groups[section_id]["children"].append(deepcopy(element))
            else:
                title_and_global.append(deepcopy(element))

        for group in section_containers:
            group["children"].sort(key=lambda item: (item.get("y", 0), item.get("priority", 0.5)))

        if template_layout.get("layout_mode") == "template_prior":
            lane_map = self._rebalance_template_block_slots(template_layout, section_containers, lane_map, state)
        lane_map = self._rebalance_soft_template_lanes(template_layout, section_containers, lane_map, state)

        lane_reports = []
        refined_elements = list(title_and_global)
        force_fit_used = False

        ordered_lane_ids = [lane["id"] for lane in template_layout["lanes"]]
        for lane_id in ordered_lane_ids:
            groups = [group for group in section_containers if group["lane_id"] == lane_id]
            groups.sort(key=lambda group: group["container"].get("y", 0))
            lane_result = self._refine_lane(groups, lane_map[lane_id], state, template_layout)
            refined_elements.extend(lane_result["elements"])
            lane_reports.append(lane_result["report"])
            force_fit_used = force_fit_used or lane_result["report"]["force_fit_used"]

        refined_elements.sort(key=lambda item: (item.get("priority", 0.5), item.get("y", 0), item.get("x", 0)))

        validation = self._validate_refined_layout(refined_elements, lane_map, state)
        report = {
            "template_name": template_layout["template_name"],
            "force_fit_used": force_fit_used,
            "lanes": lane_reports,
            "validation": validation,
            "validated_elements": len(refined_elements),
        }
        return refined_elements, report

    def _rebalance_template_block_slots(
        self,
        template_layout: Dict[str, Any],
        section_containers: List[Dict[str, Any]],
        lane_map: Dict[str, Dict[str, Any]],
        state: PosterState,
    ) -> Dict[str, Dict[str, Any]]:
        if template_layout.get("layout_mode") != "template_prior":
            return lane_map

        adjacency_graph = template_layout.get("adjacency_graph") or {}
        if not adjacency_graph:
            return lane_map

        params = {
            "section_gap": self.layout_config["section_spacing"],
            "title_to_content_gap": self.layout_config["title_to_content_spacing"],
            "visual_gap": self.layout_config["visual_spacing"]["below_visual"],
            "text_padding": self.layout_config["text_padding"]["left_right"],
            "body_font_reduction": 0,
            "title_font_reduction": 0,
            "body_font_boost": 0,
            "title_font_boost": 0,
            "visual_scale": 1.0,
        }
        demand_by_lane: Dict[str, float] = {}
        report_slots: Dict[str, Any] = {}
        for group in section_containers:
            lane_id = group["lane_id"]
            lane = dict(lane_map[lane_id])
            lane["y"] = 0.0
            lane["h"] = 1000.0
            _, section_bottom = self._layout_section(group, lane, 0.0, state, params, template_layout)
            demand = max(section_bottom, group["container"].get("height", 0.25))
            demand_by_lane[lane_id] = demand
        for lane_id, lane in lane_map.items():
            available = max(lane.get("h", 0.1), 0.1)
            pressure = demand_by_lane.get(lane_id, 0.0) / available
            report_slots[lane_id] = {
                "slot_id": lane_id,
                "demanded_height": round(demand_by_lane.get(lane_id, 0.0), 4),
                "available_height": round(available, 4),
                "pressure": round(pressure, 4),
            }

        updated = {lane_id: dict(lane) for lane_id, lane in lane_map.items()}
        max_shift_ratio = 0.10
        gutter = max(self.layout_config.get("column_spacing", 1.0) * 0.2, 0.08)
        transferred = False

        receivers = sorted(
            (lane_id for lane_id, slot in report_slots.items() if slot["pressure"] > 1.0),
            key=lambda lane_id: report_slots[lane_id]["pressure"],
            reverse=True,
        )
        for receiver_id in receivers:
            neighbors = adjacency_graph.get(receiver_id) or []
            donors = [
                neighbor for neighbor in neighbors
                if report_slots.get(neighbor["slot_id"], {}).get("pressure", 1.0) < 0.8
            ]
            donors.sort(key=lambda item: report_slots[item["slot_id"]]["pressure"])
            for donor_edge in donors:
                donor_id = donor_edge["slot_id"]
                receiver = updated[receiver_id]
                donor = updated[donor_id]
                if donor_edge.get("orientation") == "vertical":
                    transferred = self._transfer_slot_width(receiver, donor, max_shift_ratio, gutter) or transferred
                else:
                    transferred = self._transfer_slot_height(receiver, donor, max_shift_ratio, gutter) or transferred
                if transferred:
                    break
            if transferred:
                break

        if transferred:
            ordered_ids = template_layout.get("slot_order") or [lane["id"] for lane in template_layout.get("lanes", [])]
            template_layout["lanes"] = [updated[lane_id] for lane_id in ordered_ids if lane_id in updated]
            template_layout["columns"] = template_layout["lanes"]
            state["slot_pressure_report"] = {
                "slots": report_slots,
                "slot_resize_applied": True,
            }
            return updated

        state["slot_pressure_report"] = {
            "slots": report_slots,
            "slot_resize_applied": False,
        }
        return updated

    def _transfer_slot_width(self, receiver: Dict[str, Any], donor: Dict[str, Any], max_shift_ratio: float, gutter: float) -> bool:
        shift = min(donor["w"] * max_shift_ratio, donor["w"] - 1.6)
        if shift <= 0.05:
            return False
        receiver_right = receiver["x"] + receiver["w"]
        donor_right = donor["x"] + donor["w"]
        if receiver["x"] < donor["x"] and abs(receiver_right - donor["x"]) < 0.4:
            new_receiver_w = receiver["w"] + shift
            new_donor_x = donor["x"] + shift
            new_donor_w = donor["w"] - shift
            if new_donor_w <= 1.2:
                return False
            receiver["w"] = new_receiver_w
            donor["x"] = new_donor_x
            donor["w"] = new_donor_w
            return True
        if donor["x"] < receiver["x"] and abs(donor_right - receiver["x"]) < 0.4:
            new_receiver_x = receiver["x"] - shift
            new_receiver_w = receiver["w"] + shift
            new_donor_w = donor["w"] - shift
            if new_donor_w <= 1.2:
                return False
            receiver["x"] = new_receiver_x
            receiver["w"] = new_receiver_w
            donor["w"] = new_donor_w
            return True
        return False

    def _transfer_slot_height(self, receiver: Dict[str, Any], donor: Dict[str, Any], max_shift_ratio: float, gutter: float) -> bool:
        shift = min(donor["h"] * max_shift_ratio, donor["h"] - 1.4)
        if shift <= 0.05:
            return False
        receiver_bottom = receiver["y"] + receiver["h"]
        donor_bottom = donor["y"] + donor["h"]
        if receiver["y"] < donor["y"] and abs(receiver_bottom - donor["y"]) < 0.4:
            new_receiver_h = receiver["h"] + shift
            new_donor_y = donor["y"] + shift
            new_donor_h = donor["h"] - shift
            if new_donor_h <= 1.0:
                return False
            receiver["h"] = new_receiver_h
            donor["y"] = new_donor_y
            donor["h"] = new_donor_h
            return True
        if donor["y"] < receiver["y"] and abs(donor_bottom - receiver["y"]) < 0.4:
            new_receiver_y = receiver["y"] - shift
            new_receiver_h = receiver["h"] + shift
            new_donor_h = donor["h"] - shift
            if new_donor_h <= 1.0:
                return False
            receiver["y"] = new_receiver_y
            receiver["h"] = new_receiver_h
            donor["h"] = new_donor_h
            return True
        return False

    def _rebalance_soft_template_lanes(
        self,
        template_layout: Dict[str, Any],
        section_containers: List[Dict[str, Any]],
        lane_map: Dict[str, Dict[str, Any]],
        state: PosterState,
    ) -> Dict[str, Dict[str, Any]]:
        if not (
            template_layout.get("extracted_template")
            and template_layout.get("geometry_policy") == "soft"
        ):
            return lane_map

        lanes = template_layout.get("lanes") or []
        is_vertical_stack = bool(lanes) and len({round(lane.get("x", 0), 3) for lane in lanes}) == 1
        if template_layout.get("orientation") != "portrait" and not is_vertical_stack:
            return lane_map

        ordered_lanes = sorted(lanes, key=lambda lane: lane.get("y", 0))
        if len(ordered_lanes) != 3:
            return lane_map

        body_top = min(lane["y"] for lane in ordered_lanes)
        body_bottom = max(lane["y"] + lane["h"] for lane in ordered_lanes)
        existing_gaps = []
        for index in range(len(ordered_lanes) - 1):
            gap = ordered_lanes[index + 1]["y"] - (ordered_lanes[index]["y"] + ordered_lanes[index]["h"])
            if gap > 0:
                existing_gaps.append(gap)
        lane_gap = min(existing_gaps) if existing_gaps else min(self.layout_config["column_spacing"], 0.8)
        available_h = max(body_bottom - body_top - lane_gap * (len(ordered_lanes) - 1), 0.1)

        pressure_params = {
            "section_gap": self.layout_config["section_spacing"],
            "title_to_content_gap": self.layout_config["title_to_content_spacing"],
            "visual_gap": self.layout_config["visual_spacing"]["below_visual"],
            "text_padding": self.layout_config["text_padding"]["left_right"],
            "body_font_reduction": 0,
            "title_font_reduction": 0,
            "body_font_boost": 0,
            "title_font_boost": 0,
            "visual_scale": 1.0,
        }
        pressure_by_lane = {}
        for lane in ordered_lanes:
            groups = [
                group
                for group in section_containers
                if group.get("lane_id") == lane["id"]
            ]
            measured_pressure = 0.0
            probe_lane = dict(lane)
            probe_lane["y"] = 0.0
            probe_lane["h"] = 1000.0
            for index, group in enumerate(groups):
                _, section_bottom = self._layout_section(group, probe_lane, measured_pressure, state, pressure_params, template_layout)
                measured_pressure = section_bottom
                if index < len(groups) - 1:
                    measured_pressure += self.layout_config["section_spacing"]
            fallback_pressure = sum(max(group["container"].get("height", 0.0), 0.25) for group in groups)
            pressure_by_lane[lane["id"]] = max(measured_pressure, fallback_pressure, lane["h"] * 0.25)

        total_pressure = sum(pressure_by_lane.values()) or 1.0
        raw_ratios = {lane_id: pressure / total_pressure for lane_id, pressure in pressure_by_lane.items()}
        min_ratio = 0.18
        remaining = max(1.0 - min_ratio * len(ordered_lanes), 0.01)
        excess_total = sum(max(raw_ratios[lane["id"]] - min_ratio, 0.0) for lane in ordered_lanes)
        if excess_total <= 0:
            ratios = {lane["id"]: 1.0 / len(ordered_lanes) for lane in ordered_lanes}
        else:
            ratios = {
                lane["id"]: min_ratio + remaining * max(raw_ratios[lane["id"]] - min_ratio, 0.0) / excess_total
                for lane in ordered_lanes
            }

        current_y = body_top
        updated_map = dict(lane_map)
        for lane in ordered_lanes:
            updated = dict(lane)
            updated["y"] = current_y
            updated["h"] = available_h * ratios[lane["id"]]
            updated["soft_rebalanced"] = True
            current_y += updated["h"] + lane_gap
            updated_map[lane["id"]] = updated

        template_layout["lanes"] = [updated_map[lane["id"]] for lane in lanes]
        template_layout["columns"] = template_layout["lanes"]
        return updated_map

    def _assign_section_id(self, element: Dict[str, Any], section_containers: List[Dict[str, Any]], lane_map: Dict[str, Dict[str, Any]]) -> Optional[str]:
        explicit_section_id = element.get("section_id")
        if explicit_section_id and any(group["section_id"] == explicit_section_id for group in section_containers):
            return explicit_section_id

        element_id = element.get("id", "")
        if element_id:
            matches = [
                group["section_id"]
                for group in section_containers
                if element_id.startswith(f"{group['section_id']}_")
            ]
            if matches:
                return max(matches, key=len)

        lane_id = element.get("lane_id") or self._match_lane_for_element(element, lane_map)
        lane_groups = [group for group in section_containers if group["lane_id"] == lane_id]
        lane_groups.sort(key=lambda group: group["container"]["y"])
        element_y = element.get("y", 0)

        for idx, group in enumerate(lane_groups):
            start_y = group["container"]["y"]
            next_start = lane_groups[idx + 1]["container"]["y"] if idx + 1 < len(lane_groups) else lane_map[lane_id]["y"] + lane_map[lane_id]["h"] + 10
            if start_y - 0.1 <= element_y < next_start:
                return group["section_id"]
        return lane_groups[-1]["section_id"] if lane_groups else None

    def _match_lane_for_element(self, element: Dict[str, Any], lane_map: Dict[str, Dict[str, Any]]) -> str:
        element_x = element.get("x", 0)
        element_y = element.get("y", 0)
        for lane_id, lane in lane_map.items():
            within_x = lane["x"] - 0.05 <= element_x <= lane["x"] + lane["w"] + 0.05
            within_y = lane["y"] - 0.05 <= element_y <= lane["y"] + lane["h"] + 20
            if within_x and within_y:
                return lane_id
        return next(iter(lane_map))

    def _refine_lane(self, groups: List[Dict[str, Any]], lane: Dict[str, Any], state: PosterState, template_layout: Dict[str, Any]) -> Dict[str, Any]:
        if not groups:
            return {
                "elements": [],
                "report": {
                    "lane_id": lane["id"],
                    "force_fit_used": False,
                    "iterations": 0,
                    "final_overflow": 0.0,
                },
            }

        params = {
            "section_gap": self.layout_config["section_spacing"],
            "title_to_content_gap": self.layout_config["title_to_content_spacing"],
            "visual_gap": self.layout_config["visual_spacing"]["below_visual"],
            "text_padding": self.layout_config["text_padding"]["left_right"],
            "body_font_reduction": 0,
            "title_font_reduction": 0,
            "body_font_boost": 0,
            "title_font_boost": 0,
            "visual_scale": 1.0,
        }
        if self._is_soft_portrait_template(template_layout):
            params.update({
                "section_gap": 0.45,
                "title_to_content_gap": 0.2,
                "visual_gap": 0.18,
                "text_padding": 0.24,
                "body_font_reduction": 8,
                "title_font_reduction": 10,
                "visual_scale": 0.82,
            })
        params["visual_scale"] = max(
            params["visual_scale"],
            self._visual_scale_floor(groups, state, template_layout),
        )

        best_layout = None
        best_overflow = float("inf")
        best_params = deepcopy(params)

        max_iterations = self.refine_config["max_iterations"]
        if state.get("template_fast_mode"):
            max_iterations = min(int(max_iterations), 4)
        iteration_count = 0

        for iteration in range(max_iterations):
            lane_layout, overflow = self._layout_lane(groups, lane, state, params, template_layout)
            iteration_count = iteration + 1

            if overflow < best_overflow:
                best_overflow = overflow
                best_layout = lane_layout
                best_params = deepcopy(params)

            if overflow <= 0.0:
                expanded_layout, expanded_report = self._expand_underfilled_lane(
                    groups,
                    lane,
                    state,
                    template_layout,
                    params,
                    lane_layout,
                    overflow,
                )
                return {
                    "elements": expanded_layout,
                    "report": {
                        "lane_id": lane["id"],
                        "force_fit_used": False,
                        "iterations": iteration_count,
                        "final_overflow": expanded_report["overflow"],
                        "final_utilization": expanded_report["utilization"],
                        "underflow_expanded": expanded_report["expanded"],
                        "params": expanded_report["params"],
                    },
                }

            params = self._tighten_params(params, groups, state, template_layout)

        if self._is_soft_portrait_template(template_layout) and best_overflow <= 0.5:
            return {
                "elements": best_layout or [],
                "report": {
                    "lane_id": lane["id"],
                    "force_fit_used": False,
                    "iterations": iteration_count,
                    "final_overflow": best_overflow,
                    "soft_overflow_tolerated": True,
                    "params": best_params,
                },
            }

        force_fit_layout = self._force_fit_lane(best_layout or [], lane, state, template_layout)
        return {
            "elements": force_fit_layout,
            "report": {
                "lane_id": lane["id"],
                "force_fit_used": True,
                "iterations": iteration_count,
                "final_overflow": best_overflow,
                "params": best_params,
            },
        }

    def _tighten_params(
        self,
        params: Dict[str, Any],
        groups: Optional[List[Dict[str, Any]]] = None,
        state: Optional[PosterState] = None,
        template_layout: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        tightened = deepcopy(params)
        tightened["section_gap"] = max(
            self.refine_config["min_section_gap"],
            tightened["section_gap"] - self.refine_config["section_gap_step"],
        )
        tightened["title_to_content_gap"] = max(
            self.refine_config["min_title_to_content_gap"],
            tightened["title_to_content_gap"] - 0.05,
        )
        tightened["visual_gap"] = max(
            self.refine_config["min_visual_gap"],
            tightened["visual_gap"] - 0.04,
        )
        tightened["text_padding"] = max(
            self.refine_config["min_text_padding"],
            tightened["text_padding"] - 0.02,
        )
        tightened["body_font_reduction"] += self.refine_config["body_font_shrink_step"]
        tightened["title_font_reduction"] += self.refine_config["title_font_shrink_step"]
        min_visual_scale = self.refine_config["min_visual_scale"]
        if groups is not None and state is not None and template_layout is not None:
            min_visual_scale = max(
                min_visual_scale,
                self._visual_scale_floor(groups, state, template_layout),
            )
        tightened["visual_scale"] = max(
            min_visual_scale,
            tightened["visual_scale"] - self.refine_config["visual_scale_step"],
        )
        return tightened

    def _loosen_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        loosened = deepcopy(params)
        loosened["section_gap"] = min(
            self.refine_config.get("max_section_gap", self.layout_config["section_spacing"]),
            loosened["section_gap"] + self.refine_config.get("section_gap_expand_step", 0.12),
        )
        loosened["body_font_boost"] = min(
            self.refine_config.get("max_body_font_boost", 0),
            loosened.get("body_font_boost", 0) + self.refine_config.get("body_font_boost_step", 2),
        )
        loosened["title_font_boost"] = min(
            self.refine_config.get("max_section_title_font_boost", 0),
            loosened.get("title_font_boost", 0) + self.refine_config.get("title_font_boost_step", 1),
        )
        loosened["visual_scale"] = min(
            self.refine_config.get("max_visual_scale", 1.0),
            loosened["visual_scale"] + self.refine_config.get("visual_scale_step", 0.05),
        )
        return loosened

    def _expand_underfilled_lane(
        self,
        groups: List[Dict[str, Any]],
        lane: Dict[str, Any],
        state: PosterState,
        template_layout: Dict[str, Any],
        params: Dict[str, Any],
        lane_layout: List[Dict[str, Any]],
        overflow: float,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        lane_height = lane["h"]
        used_height = lane_height + overflow
        utilization = used_height / max(lane_height, 0.01)
        target_utilization = self.refine_config.get("target_lane_utilization", 0.9)
        free_space = -overflow

        best_layout = lane_layout
        best_params = deepcopy(params)
        best_overflow = overflow
        best_utilization = utilization

        if free_space < self.refine_config.get("underflow_expand_threshold", 1.0) or utilization >= target_utilization:
            return best_layout, {
                "expanded": False,
                "overflow": best_overflow,
                "utilization": best_utilization,
                "params": best_params,
            }

        trial_params = deepcopy(params)
        for _ in range(self.refine_config.get("max_underflow_iterations", 8)):
            trial_params = self._loosen_params(trial_params)
            candidate_layout, candidate_overflow = self._layout_lane(groups, lane, state, trial_params, template_layout)
            if candidate_overflow > 0.0:
                break

            candidate_used = lane_height + candidate_overflow
            candidate_utilization = candidate_used / max(lane_height, 0.01)
            if candidate_utilization > best_utilization:
                best_layout = candidate_layout
                best_params = deepcopy(trial_params)
                best_overflow = candidate_overflow
                best_utilization = candidate_utilization

            if candidate_utilization >= target_utilization:
                break

            if trial_params == self._loosen_params(trial_params):
                break

        return best_layout, {
            "expanded": best_layout is not lane_layout,
            "overflow": best_overflow,
            "utilization": best_utilization,
            "params": best_params,
        }

    def _layout_lane(self, groups: List[Dict[str, Any]], lane: Dict[str, Any], state: PosterState, params: Dict[str, Any], template_layout: Dict[str, Any]) -> tuple[List[Dict[str, Any]], float]:
        elements: List[Dict[str, Any]] = []
        current_y = lane["y"]

        for index, group in enumerate(groups):
            section_elements, section_bottom = self._layout_section(group, lane, current_y, state, params, template_layout)
            elements.extend(section_elements)
            current_y = section_bottom
            if index < len(groups) - 1:
                current_y += params["section_gap"]

        lane_bottom = lane["y"] + lane["h"]
        overflow = current_y - lane_bottom
        return elements, overflow

    def _layout_section(self, group: Dict[str, Any], lane: Dict[str, Any], section_y: float, state: PosterState, params: Dict[str, Any], template_layout: Dict[str, Any]) -> tuple[List[Dict[str, Any]], float]:
        container = deepcopy(group["container"])
        children = [deepcopy(child) for child in group["children"]]
        section_id = group["section_id"]

        title_elements = [child for child in children if child.get("type") in {"section_title", "title_accent_block", "title_accent_line"}]
        visual_elements = [child for child in children if child.get("type") == "visual"]
        text_elements = [child for child in children if child.get("type") == "text"]
        other_elements = [child for child in children if child.get("type") not in {"section_title", "title_accent_block", "title_accent_line", "visual", "text"}]

        original_section_y = container.get("y", section_y)
        current_y = section_y
        rebuilt_children: List[Dict[str, Any]] = []
        content_bottom = current_y

        section_title_element = next((child for child in title_elements if child.get("type") == "section_title"), None)
        if section_title_element:
            original_font_size = int(section_title_element.get("font_size", self.typography_config["sizes"]["section_title"]))
            title_font_size = max(
                self._min_section_title_font_size(template_layout),
                original_font_size - params["title_font_reduction"] + params.get("title_font_boost", 0),
            )
            title_font_size = min(
                self.refine_config.get("max_section_title_font_size", title_font_size),
                title_font_size,
            )
            title_scale = title_font_size / max(original_font_size, 1)
            title_x_offset = section_title_element.get("x", lane["x"]) - container.get("x", lane["x"])
            title_height = max((title_font_size / 72) + 0.05, section_title_element.get("height", 0.8) * title_scale)

            for child in title_elements:
                child_type = child.get("type")
                child_x_offset = child.get("x", lane["x"]) - container.get("x", lane["x"])
                child_y_offset = max(child.get("y", original_section_y) - original_section_y, 0.0) * title_scale

                if child_type == "section_title":
                    child["x"] = lane["x"] + child_x_offset
                    child["y"] = section_y + child_y_offset
                    child["height"] = title_height
                    child["width"] = max(lane["w"] - (child["x"] - lane["x"]) - params["text_padding"], 0.5)
                    child["font_size"] = title_font_size
                else:
                    child["x"] = lane["x"] + child_x_offset
                    child["y"] = section_y + child_y_offset
                    child["height"] = max(child.get("height", 0.3) * title_scale, 0.08)
                    if child_type in {"title_accent_block", "title_accent_line"}:
                        child["x"] = lane["x"]
                        child["width"] = lane["w"]
                    else:
                        child["width"] = max(child.get("width", 0.3) * title_scale, 0.08)
                rebuilt_children.append(child)
                content_bottom = max(content_bottom, child["y"] + child["height"])

            current_y = content_bottom + params["title_to_content_gap"]

        visual_priority_tail = self._layout_cluster_72_visual_priority_tail(
            visual_elements,
            text_elements,
            lane,
            current_y,
            content_bottom,
            state,
            template_layout,
        )
        if visual_priority_tail:
            tail_elements, current_y, content_bottom = visual_priority_tail
            rebuilt_children.extend(tail_elements)
            visual_elements = []
            text_elements = []
        else:
            split_tail = self._layout_portrait_split_visual_text(
                visual_elements,
                text_elements,
                lane,
                current_y,
                state,
                params,
                template_layout,
            )
            if split_tail:
                tail_elements, current_y, content_bottom = split_tail
                rebuilt_children.extend(tail_elements)
                visual_elements = []
                text_elements = []
            else:
                visual_available_width = self._get_visual_width_for_lane(lane, state, template_layout, params)
                for visual in visual_elements:
                    lane_for_footprint = self._lane_with_poster_orientation(lane, state, template_layout)
                    aspect_ratio = visual.get("width", 1.0) / max(visual.get("height", 0.01), 0.01)
                    scaled_width = min(visual_available_width, visual.get("width", visual_available_width) * params["visual_scale"])
                    scaled_height = scaled_width / max(aspect_ratio, 0.01)
                    scaled_width, scaled_height, footprint_report = enforce_visual_footprint(
                        visual.get("visual_id") or visual.get("id"),
                        scaled_width,
                        scaled_height,
                        visual_available_width,
                        lane_for_footprint,
                        state,
                        self.config,
                    )

                    visual["width"] = scaled_width
                    visual["height"] = scaled_height
                    visual["x"] = lane["x"] + (lane["w"] - scaled_width) / 2
                    visual["y"] = current_y
                    visual["visual_footprint"] = footprint_report

                    rebuilt_children.append(visual)
                    current_y = visual["y"] + visual["height"] + params["visual_gap"]
                    content_bottom = max(content_bottom, visual["y"] + visual["height"])

        for text_element in text_elements:
            original_font_size = int(text_element.get("font_size", self.typography_config["sizes"]["body_text"]))
            font_size = max(
                self._min_body_font_size(template_layout),
                original_font_size - params["body_font_reduction"] + params.get("body_font_boost", 0),
            )
            font_size = min(self.refine_config.get("max_body_font_size", font_size), font_size)
            text_width = max(lane["w"] - 2 * params["text_padding"], 0.5)
            plain_text = self._strip_markup_for_measurement(text_element.get("content", ""))
            measured = self._measure_text_height_for_refinement(
                text_content=plain_text,
                width_inches=text_width,
                font_name=text_element.get("font_family", self.typography_config["fonts"]["body_text"]),
                font_size=font_size,
                line_spacing=text_element.get("line_spacing", 1.0),
                template_layout=template_layout,
            )

            text_element["x"] = lane["x"] + params["text_padding"]
            text_element["y"] = current_y
            text_element["width"] = text_width
            text_element["height"] = (
                measured["optimal_height"] * self.refine_config.get("text_height_safety_factor", 1.0)
                + self.refine_config.get("text_height_safety_padding", 0.05)
            )
            text_element["font_size"] = font_size

            rebuilt_children.append(text_element)
            current_y = text_element["y"] + text_element["height"]
            content_bottom = max(content_bottom, text_element["y"] + text_element["height"])

        for other in other_elements:
            other["x"] = lane["x"] + (other.get("x", lane["x"]) - container.get("x", lane["x"]))
            other["y"] = section_y + max(other.get("y", original_section_y) - original_section_y, 0.0)
            rebuilt_children.append(other)
            content_bottom = max(content_bottom, other["y"] + other.get("height", 0))

        container["x"] = lane["x"]
        container["y"] = section_y
        container["width"] = lane["w"]
        container["height"] = max(
            content_bottom - section_y + self.refine_config.get("container_bottom_padding", 0.0),
            0.25,
        )

        return [container] + rebuilt_children, container["y"] + container["height"]

    def _layout_portrait_split_visual_text(
        self,
        visual_elements: List[Dict[str, Any]],
        text_elements: List[Dict[str, Any]],
        lane: Dict[str, Any],
        current_y: float,
        state: PosterState,
        params: Dict[str, Any],
        template_layout: Dict[str, Any],
    ) -> Optional[tuple[List[Dict[str, Any]], float, float]]:
        if not self._should_use_portrait_split_layout(visual_elements, text_elements, lane, state, template_layout):
            return None

        cfg = visual_footprint_config(self.config)
        padding = max(float(params.get("text_padding", 0.24)), 0.18)
        gap = float(cfg.get("portrait_split_gap_inches", 0.45) or 0.45)
        bottom_padding = float(cfg.get("portrait_split_bottom_padding_inches", 0.10) or 0.10)
        lane_bottom = float(lane["y"]) + float(lane["h"])
        available_height = max(lane_bottom - current_y - bottom_padding, 0.0)
        if available_height < float(cfg.get("portrait_split_min_height_inches", 4.8) or 4.8):
            return None

        usable_width = max(float(lane["w"]) - 2 * padding, 0.1)
        max_visual_width = min(
            usable_width * float(cfg.get("portrait_split_visual_width_fraction", 0.48) or 0.48),
            usable_width - gap - float(cfg.get("portrait_split_min_text_width_inches", 8.0) or 8.0),
        )
        if max_visual_width <= 0:
            return None

        visual = deepcopy(visual_elements[0])
        aspect_ratio = max(float(visual.get("width", 1.0)) / max(float(visual.get("height", 0.01)), 0.01), 0.2)
        target_width = min(max_visual_width, available_height * aspect_ratio)
        target_height = target_width / aspect_ratio
        lane_for_footprint = self._lane_with_poster_orientation(lane, state, template_layout)
        scaled_width, scaled_height, footprint_report = enforce_visual_footprint(
            visual.get("visual_id") or visual.get("id"),
            target_width,
            target_height,
            max_visual_width,
            lane_for_footprint,
            state,
            self.config,
        )
        if not footprint_report.get("ok"):
            return None

        text_width = usable_width - scaled_width - gap
        if text_width < float(cfg.get("portrait_split_min_text_width_inches", 8.0) or 8.0):
            return None

        laid_out_text = self._measure_split_text_elements(
            text_elements,
            text_width,
            available_height,
            params,
            template_layout,
        )
        if laid_out_text is None:
            return None

        measured_text, total_text_height = laid_out_text
        visual_on_left = self._split_visual_on_left(visual, lane)
        content_left = float(lane["x"]) + padding
        if visual_on_left:
            visual_x = content_left
            text_x = visual_x + scaled_width + gap
        else:
            text_x = content_left
            visual_x = text_x + text_width + gap

        visual["x"] = visual_x
        visual["y"] = current_y + max((available_height - scaled_height) / 2, 0.0)
        visual["width"] = scaled_width
        visual["height"] = scaled_height
        visual["visual_footprint"] = footprint_report
        visual["portrait_split_layout"] = "image_left_text_right" if visual_on_left else "text_left_image_right"

        y = current_y + max((available_height - total_text_height) / 2, 0.0)
        tail: List[Dict[str, Any]] = [visual]
        content_bottom = visual["y"] + visual["height"]
        for text_element, text_height, font_size in measured_text:
            item = deepcopy(text_element)
            item["x"] = text_x
            item["y"] = y
            item["width"] = text_width
            item["height"] = text_height
            item["font_size"] = font_size
            item["portrait_split_layout"] = visual["portrait_split_layout"]
            tail.append(item)
            y += text_height
            content_bottom = max(content_bottom, item["y"] + item["height"])

        current_y = max(content_bottom, lane_bottom - bottom_padding)
        return tail, current_y, max(content_bottom, current_y)

    def _should_use_portrait_split_layout(
        self,
        visual_elements: List[Dict[str, Any]],
        text_elements: List[Dict[str, Any]],
        lane: Dict[str, Any],
        state: PosterState,
        template_layout: Dict[str, Any],
    ) -> bool:
        if not visual_elements or not text_elements or len(visual_elements) != 1:
            return False
        if self._poster_orientation(state, template_layout) != "portrait":
            return False
        visual_id = str(visual_elements[0].get("visual_id") or visual_elements[0].get("id") or "")
        if visual_id.startswith("table_"):
            return False

        cfg = visual_footprint_config(self.config)
        width = float(lane.get("w", 0.0) or 0.0)
        height = float(lane.get("h", 0.0) or 0.0)
        if width < float(cfg.get("portrait_split_min_width_inches", 18.0) or 18.0):
            return False
        if height < float(cfg.get("portrait_split_min_height_inches", 4.8) or 4.8):
            return False
        return width / max(height, 0.01) >= float(cfg.get("portrait_split_min_aspect", 2.35) or 2.35)

    def _measure_split_text_elements(
        self,
        text_elements: List[Dict[str, Any]],
        text_width: float,
        available_height: float,
        params: Dict[str, Any],
        template_layout: Dict[str, Any],
    ) -> Optional[tuple[List[tuple[Dict[str, Any], float, int]], float]]:
        base_size = int(text_elements[0].get("font_size", self.typography_config["sizes"]["body_text"]))
        preferred = max(
            self._min_body_font_size(template_layout),
            base_size - int(params.get("body_font_reduction", 0)) + int(params.get("body_font_boost", 0)),
        )
        min_size = self._min_body_font_size(template_layout)

        for font_size in range(preferred, min_size - 1, -2):
            measured: List[tuple[Dict[str, Any], float, int]] = []
            total_height = 0.0
            for text_element in text_elements:
                plain_text = self._strip_markup_for_measurement(text_element.get("content", ""))
                result = self._measure_text_height_for_refinement(
                    text_content=plain_text,
                    width_inches=text_width,
                    font_name=text_element.get("font_family", self.typography_config["fonts"]["body_text"]),
                    font_size=font_size,
                    line_spacing=text_element.get("line_spacing", 1.0),
                    template_layout=template_layout,
                )
                text_height = (
                    result["optimal_height"] * self.refine_config.get("text_height_safety_factor", 1.0)
                    + self.refine_config.get("text_height_safety_padding", 0.05)
                )
                measured.append((text_element, text_height, font_size))
                total_height += text_height
            if total_height <= available_height + 0.05:
                return measured, total_height

        return None

    def _split_visual_on_left(self, visual: Dict[str, Any], lane: Dict[str, Any]) -> bool:
        visual_center = float(visual.get("x", lane.get("x", 0.0))) + float(visual.get("width", 0.0)) / 2
        lane_center = float(lane.get("x", 0.0)) + float(lane.get("w", 0.0)) / 2
        if abs(visual_center - lane_center) < 0.2:
            return True
        return visual_center <= lane_center

    def _lane_with_poster_orientation(
        self,
        lane: Dict[str, Any],
        state: PosterState,
        template_layout: Dict[str, Any],
    ) -> Dict[str, Any]:
        lane_for_footprint = dict(lane)
        lane_for_footprint.setdefault("poster_orientation", self._poster_orientation(state, template_layout))
        return lane_for_footprint

    def _poster_orientation(self, state: PosterState, template_layout: Dict[str, Any]) -> str:
        orientation = str(template_layout.get("orientation") or "").lower()
        if orientation:
            return orientation
        return (
            "portrait"
            if float(state.get("poster_height", 0.0) or 0.0) > float(state.get("poster_width", 0.0) or 0.0)
            else "landscape"
        )

    def _layout_cluster_72_visual_priority_tail(
        self,
        visual_elements: List[Dict[str, Any]],
        text_elements: List[Dict[str, Any]],
        lane: Dict[str, Any],
        current_y: float,
        title_bottom: float,
        state: PosterState,
        template_layout: Dict[str, Any],
    ) -> Optional[tuple[List[Dict[str, Any]], float, float]]:
        if not self._is_cluster_72_visual_priority_lane(lane, state, template_layout, visual_elements):
            return None

        padding = float(self.config.get("template_fast_mode", {}).get("visual_priority_text_padding", 0.18))
        visual_gap = float(self.config.get("template_fast_mode", {}).get("visual_priority_gap", 0.08))
        bottom_padding = float(self.config.get("template_fast_mode", {}).get("visual_priority_bottom_padding", 0.08))
        caption_font_size = int(self.config.get("template_fast_mode", {}).get("visual_priority_caption_font_size", 30))
        caption_line_spacing = 0.95
        text_width = max(lane["w"] - 2 * padding, 0.5)

        measured_text: List[tuple[Dict[str, Any], float]] = []
        for text_element in text_elements:
            plain_text = self._strip_markup_for_measurement(text_element.get("content", ""))
            measured = self._measure_text_height_for_refinement(
                text_content=plain_text,
                width_inches=text_width,
                font_name=text_element.get("font_family", self.typography_config["fonts"]["body_text"]),
                font_size=caption_font_size,
                line_spacing=caption_line_spacing,
                template_layout=template_layout,
            )
            text_height = (
                measured["optimal_height"] * 1.03
                + min(self.refine_config.get("text_height_safety_padding", 0.05), 0.08)
            )
            measured_text.append((text_element, min(max(text_height, 0.42), 1.05)))

        total_text_height = sum(height for _, height in measured_text)
        if measured_text:
            total_text_height += visual_gap

        lane_bottom = lane["y"] + lane["h"]
        visual_top = max(title_bottom + visual_gap, lane["y"])
        max_visual_height = max(lane_bottom - visual_top - total_text_height - bottom_padding, 0.4)
        visual_available_width = max(lane["w"] - 2 * padding, 0.4)
        tail: List[Dict[str, Any]] = []
        content_bottom = title_bottom
        y = visual_top

        for visual in visual_elements[:1]:
            aspect_ratio = visual.get("width", 1.0) / max(visual.get("height", 0.01), 0.01)
            scaled_width = min(visual_available_width, max_visual_height * max(aspect_ratio, 0.01))
            scaled_height = scaled_width / max(aspect_ratio, 0.01)
            visual["width"] = scaled_width
            visual["height"] = scaled_height
            visual["x"] = lane["x"] + (lane["w"] - scaled_width) / 2
            visual["y"] = y
            visual["visual_priority_layout"] = "cluster_72_fast"
            tail.append(visual)
            y = visual["y"] + visual["height"]
            content_bottom = max(content_bottom, y)

        if measured_text:
            y += visual_gap
        for text_element, text_height in measured_text:
            text_element["x"] = lane["x"] + padding
            text_element["y"] = y
            text_element["width"] = text_width
            text_element["height"] = text_height
            text_element["font_size"] = caption_font_size
            text_element["line_spacing"] = caption_line_spacing
            text_element["visual_priority_caption"] = True
            tail.append(text_element)
            y = text_element["y"] + text_element["height"]
            content_bottom = max(content_bottom, y)

        return tail, y, content_bottom

    def _is_cluster_72_visual_priority_lane(
        self,
        lane: Dict[str, Any],
        state: PosterState,
        template_layout: Dict[str, Any],
        visual_elements: List[Dict[str, Any]],
    ) -> bool:
        if not visual_elements:
            return False
        if not state.get("template_fast_mode"):
            return False
        if str(template_layout.get("template_name") or state.get("resolved_layout_template") or "") != "cluster_72":
            return False
        figure_slots = set(((state.get("fast_visual_policy") or {}).get("figure_slots") or ["slot_2", "slot_3"]))
        lane_id = str(lane.get("id") or lane.get("region_id") or lane.get("slot_id") or "")
        if lane_id not in figure_slots:
            return False
        return any(str(visual.get("visual_id") or "").startswith("figure_") for visual in visual_elements)

    def _measure_text_height_for_refinement(
        self,
        text_content: str,
        width_inches: float,
        font_name: str,
        font_size: int,
        line_spacing: float,
        template_layout: Dict[str, Any],
    ) -> Dict[str, float]:
        if template_layout.get("extracted_template") or template_layout.get("orientation") == "portrait":
            return {
                "optimal_height": self._estimate_text_height_fast(
                    text_content,
                    width_inches,
                    font_size,
                    line_spacing,
                    template_layout,
                )
            }
        return measure_text_height(
            text_content=text_content,
            width_inches=width_inches,
            font_name=font_name,
            font_size=font_size,
            line_spacing=line_spacing,
        )

    def _estimate_text_height_fast(
        self,
        text_content: str,
        width_inches: float,
        font_size: int,
        line_spacing: float,
        template_layout: Optional[Dict[str, Any]] = None,
    ) -> float:
        chars_per_inch = self._chars_per_inch_for_template(template_layout)
        chars_per_line = max(int(width_inches * chars_per_inch * (44 / max(font_size, 1))), 18)
        line_count = 0
        for raw_line in text_content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line_count += max(1, (len(line) + chars_per_line - 1) // chars_per_line)
        if line_count == 0:
            return 0.2
        line_height = (font_size / 72) * max(line_spacing, 0.9) * 1.15
        return line_count * line_height + max(line_count - 1, 0) * 0.04

    def _chars_per_inch_for_template(self, template_layout: Optional[Dict[str, Any]] = None) -> float:
        default = float(self.refine_config.get("ppt_chars_per_inch_at_44pt", 4.2))
        if template_layout and str(template_layout.get("orientation") or "").lower() == "portrait":
            return float(self.refine_config.get("portrait_ppt_chars_per_inch_at_44pt", default))
        return default

    def _get_visual_width_for_lane(self, lane: Dict[str, Any], state: PosterState, template_layout: Dict[str, Any], params: Dict[str, Any]) -> float:
        visual_width = lane["w"] - 2 * params["text_padding"]
        visual_width_cap = template_layout.get("visual_width_cap")
        if visual_width_cap:
            visual_width = min(visual_width, visual_width_cap * params["visual_scale"])
        return max(visual_width, 0.4)

    def _visual_scale_floor(
        self,
        groups: List[Dict[str, Any]],
        state: PosterState,
        template_layout: Dict[str, Any],
    ) -> float:
        if not state.get("template_fast_mode"):
            return float(self.refine_config.get("min_visual_scale", 0.72))
        if not any(
            child.get("type") == "visual"
            for group in groups
            for child in group.get("children", [])
        ):
            return float(self.refine_config.get("min_visual_scale", 0.72))
        cfg = visual_footprint_config(self.config)
        floor = float(cfg.get("min_visual_scale_in_visual_blocks", 0.95) or 0.95)
        has_key_visual = any(
            int((group.get("container") or {}).get("importance_level") or 2) <= 1
            for group in groups
            if any(child.get("type") == "visual" for child in group.get("children", []))
        )
        if has_key_visual:
            floor = max(floor, float(cfg.get("key_visual_min_scale", 1.0) or 1.0))
        if self._is_soft_portrait_template(template_layout):
            floor = min(floor, 0.95)
        return max(float(self.refine_config.get("min_visual_scale", 0.72)), floor)

    def _is_soft_portrait_template(self, template_layout: Dict[str, Any]) -> bool:
        lanes = template_layout.get("lanes") or []
        is_vertical_stack = bool(lanes) and len({round(lane.get("x", 0), 3) for lane in lanes}) == 1
        return (
            template_layout.get("extracted_template")
            and template_layout.get("geometry_policy") == "soft"
            and (template_layout.get("orientation") == "portrait" or is_vertical_stack)
        )

    def _min_body_font_size(self, template_layout: Dict[str, Any]) -> int:
        if self._is_soft_portrait_template(template_layout):
            return 24
        return self.refine_config["min_body_font_size"]

    def _min_section_title_font_size(self, template_layout: Dict[str, Any]) -> int:
        if self._is_soft_portrait_template(template_layout):
            return 32
        return self.refine_config["min_section_title_font_size"]

    def _strip_markup_for_measurement(self, content: str) -> str:
        text = re.sub(r"<color:[^>]+>", "", content)
        text = text.replace("</color>", "")
        text = text.replace("**", "")
        text = text.replace("*", "")
        return text

    def _force_fit_lane(self, lane_layout: List[Dict[str, Any]], lane: Dict[str, Any], state: PosterState, template_layout: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not lane_layout:
            return lane_layout

        max_bottom = max(element.get("y", 0) + element.get("height", 0) for element in lane_layout)
        lane_bottom = lane["y"] + lane["h"]
        used_height = max_bottom - lane["y"]
        if used_height <= 0:
            return lane_layout

        compression_ratio = min(1.0, lane["h"] / used_height)
        if compression_ratio >= 0.999:
            return lane_layout

        compressed = []
        for element in lane_layout:
            item = deepcopy(element)
            relative_y = item.get("y", lane["y"]) - lane["y"]
            item["y"] = lane["y"] + relative_y * compression_ratio
            item["height"] = max(item.get("height", 0.0) * compression_ratio, 0.05)

            if item.get("type") == "visual":
                original_width = item.get("width", 0.5)
                new_width = max(original_width * compression_ratio, 0.25)
                center_x = lane["x"] + lane["w"] / 2
                item["width"] = new_width
                item["x"] = center_x - new_width / 2
                lane_for_footprint = dict(lane)
                lane_for_footprint.setdefault(
                    "poster_orientation",
                    template_layout.get("orientation")
                    or (
                        "portrait"
                        if float(state.get("poster_height", 0.0) or 0.0) > float(state.get("poster_width", 0.0) or 0.0)
                        else "landscape"
                    ),
                )
                max_visual_width = max(lane["w"] - 2 * self.refine_config.get("min_text_padding", 0.18), 0.4)
                protected_width, protected_height, footprint_report = enforce_visual_footprint(
                    item.get("visual_id") or item.get("id"),
                    item["width"],
                    item["height"],
                    max_visual_width,
                    lane_for_footprint,
                    state,
                    self.config,
                )
                item["width"] = protected_width
                item["height"] = protected_height
                item["x"] = center_x - protected_width / 2
                item["visual_footprint"] = footprint_report
            elif item.get("type") in {"title_accent_block", "title_accent_line"}:
                item["x"] = lane["x"]
                item["width"] = lane["w"]
            elif item.get("type") == "text":
                item["font_size"] = max(
                    self._min_body_font_size(template_layout),
                    int(round(item.get("font_size", 44) * compression_ratio)),
                )
                item["height"] = max(
                    item.get("height", 0.0),
                    self._measured_text_box_height(item, template_layout),
                )
            elif item.get("type") == "section_title":
                item["font_size"] = max(
                    self._min_section_title_font_size(template_layout),
                    int(round(item.get("font_size", 64) * compression_ratio)),
                )
                item["height"] = max(
                    item.get("height", 0.0),
                    (item["font_size"] / 72) + 0.05,
                )

            compressed.append(item)
        return self._sync_container_bounds(compressed)

    def _measured_text_box_height(self, item: Dict[str, Any], template_layout: Dict[str, Any]) -> float:
        plain_text = self._strip_markup_for_measurement(str(item.get("content") or ""))
        measured = self._measure_text_height_for_refinement(
            text_content=plain_text,
            width_inches=max(float(item.get("width", 0.0) or 0.0), 0.5),
            font_name=item.get("font_family", self.typography_config["fonts"]["body_text"]),
            font_size=int(item.get("font_size") or self.typography_config["sizes"]["body_text"]),
            line_spacing=float(item.get("line_spacing", 1.0) or 1.0),
            template_layout=template_layout,
        )
        return (
            float(measured["optimal_height"]) * self.refine_config.get("text_height_safety_factor", 1.0)
            + self.refine_config.get("text_height_safety_padding", 0.05)
        )

    def _sync_container_bounds(self, elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        containers = {
            str(element.get("section_id")): element
            for element in elements
            if element.get("type") == "section_container" and element.get("section_id")
        }
        if not containers:
            return elements

        for element in elements:
            if element.get("type") == "section_container":
                continue
            section_id = str(element.get("section_id") or "")
            parent = containers.get(section_id)
            if not parent:
                element_id = str(element.get("id") or element.get("slot_id") or "")
                matches = [candidate for candidate in containers if element_id.startswith(f"{candidate}_")]
                parent = containers.get(max(matches, key=len)) if matches else None
            if not parent:
                continue
            child_bottom = float(element.get("y", 0.0) or 0.0) + float(element.get("height", 0.0) or 0.0)
            required = child_bottom - float(parent.get("y", 0.0) or 0.0) + self.refine_config.get("container_bottom_padding", 0.0)
            parent["height"] = max(float(parent.get("height", 0.0) or 0.0), required)
        return elements

    def _validate_refined_layout(self, elements: List[Dict[str, Any]], lane_map: Dict[str, Dict[str, Any]], state: PosterState) -> Dict[str, Any]:
        issues = []
        slide_width = state["poster_width"]
        slide_height = state["poster_height"]

        section_containers = [element for element in elements if element.get("type") == "section_container"]
        container_by_section = {
            section.get("section_id"): section
            for section in section_containers
            if section.get("section_id")
        }
        for element in elements:
            x = element.get("x", 0)
            y = element.get("y", 0)
            width = element.get("width", 0)
            height = element.get("height", 0)
            if x < 0 or y < 0 or x + width > slide_width + 1e-6 or y + height > slide_height + 1e-6:
                issues.append(f"element overflow: {element.get('type')} {element.get('id', element.get('section_id', 'unknown'))}")

            parent = self._find_parent_container(element, container_by_section)
            if parent and element.get("type") != "section_container":
                tolerance = 0.03
                parent_right = parent.get("x", 0) + parent.get("width", 0)
                parent_bottom = parent.get("y", 0) + parent.get("height", 0)
                if x < parent.get("x", 0) - tolerance or x + width > parent_right + tolerance:
                    issues.append(f"child horizontal overflow in section {parent.get('section_id')}: {element.get('id', element.get('type'))}")
                if y < parent.get("y", 0) - tolerance or y + height > parent_bottom + tolerance:
                    issues.append(f"child vertical overflow in section {parent.get('section_id')}: {element.get('id', element.get('type'))}")

        for lane_id, lane in lane_map.items():
            lane_sections = [section for section in section_containers if section.get("lane_id") == lane_id]
            lane_sections.sort(key=lambda item: item.get("y", 0))
            previous_bottom = lane["y"]
            lane_tolerance = 0.5 if self._is_soft_portrait_template(state.get("layout_template_metadata") or {}) else 0.02
            for section in lane_sections:
                if section["y"] < previous_bottom - 0.02:
                    issues.append(f"section overlap in lane {lane_id}: {section.get('section_id')}")
                if section["y"] + section["height"] > lane["y"] + lane["h"] + lane_tolerance:
                    issues.append(f"lane overflow in {lane_id}: {section.get('section_id')}")
                previous_bottom = max(previous_bottom, section["y"] + section["height"])

        fixed_template = (state.get("layout_template_metadata") or {}).get("layout_mode") == "template_prior"
        if fixed_template:
            for index, left in enumerate(section_containers):
                for right in section_containers[index + 1:]:
                    if self._section_boxes_overlap(left, right):
                        issues.append(
                            "section container overlap: "
                            f"{left.get('section_id')} and {right.get('section_id')}"
                        )

        return {"issues": issues}

    def _section_boxes_overlap(self, left: Dict[str, Any], right: Dict[str, Any]) -> bool:
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

    def _find_parent_container(self, element: Dict[str, Any], container_by_section: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        section_id = element.get("section_id")
        if section_id in container_by_section:
            return container_by_section[section_id]

        element_id = str(element.get("id") or element.get("slot_id") or "")
        matches = [
            section_id
            for section_id in container_by_section
            if element_id.startswith(f"{section_id}_")
        ]
        if matches:
            return container_by_section[max(matches, key=len)]
        return None

    def _save_outputs(self, state: PosterState, report: Dict[str, Any]):
        output_dir = Path(state["output_dir"]) / "content"
        output_dir.mkdir(parents=True, exist_ok=True)

        with open(output_dir / "styled_layout.json", "w", encoding="utf-8") as f:
            json.dump(state.get("styled_layout", []), f, indent=2)

        with open(output_dir / "micro_layout_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)


def micro_layout_refiner_node(state: PosterState) -> Dict[str, Any]:
    result = MicroLayoutRefiner()(state)
    return {
        **state,
        "styled_layout": result.get("styled_layout"),
        "slot_pressure_report": result.get("slot_pressure_report"),
        "draft_status": result.get("draft_status", state.get("draft_status", "pending")),
        "final_poster_accepted": result.get("final_poster_accepted", False),
        "draft_rejection_reason": result.get("draft_rejection_reason"),
        "current_agent": result.get("current_agent"),
        "errors": result.get("errors", []),
    }
