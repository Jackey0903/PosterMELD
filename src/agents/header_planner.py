"""Header route planning for poster title, authors, and logos."""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config.poster_config import load_config
from src.state.poster_state import PosterState
from src.tools.layout_api import LayoutTemplates
from src.utils.text_cleanup import normalize_text_for_poster, normalize_title_for_poster
from utils.src.logging_utils import log_agent_error, log_agent_info, log_agent_success, log_agent_warning


class HeaderPlanner:
    """Plan a single safe header composition route before layout rendering."""

    VALID_ROUTES = {"auto", "classic_left", "centered", "right_title", "split_logos"}
    VALID_SUBTITLE_POLICIES = {"auto", "off", "always"}

    def __init__(self):
        self.name = "header_planner"
        self.config = load_config()
        self.header_config = self.config.get("header_planner", {})

    def __call__(self, state: PosterState) -> PosterState:
        log_agent_info(self.name, "planning poster header route")

        try:
            if not self.header_config.get("enabled", True):
                state["header_plan"] = None
                state["current_agent"] = self.name
                return state

            template_layout = self._resolve_template_layout(state)
            title, authors = self._title_and_authors(state)
            aff_logos = self._collect_affiliation_logos(state)
            has_conf = bool(state.get("logo_path") and Path(str(state["logo_path"])).exists())

            rng = self._rng(state, title)
            route = self._select_route(state, template_layout, has_conf, aff_logos, rng)
            subtitle_text = self._select_subtitle(state, title, rng)
            plan = self._build_plan(
                state=state,
                template_layout=template_layout,
                route=route,
                title=title,
                authors=authors,
                subtitle_text=subtitle_text,
                aff_logos=aff_logos,
                has_conf=has_conf,
                logo_scale=1.0,
                fallback=False,
            )

            if not plan["validation"]["passed"]:
                log_agent_warning(self.name, f"header route '{route}' failed validation: {plan['validation']['reason']}")
                plan = self._build_plan(
                    state=state,
                    template_layout=template_layout,
                    route="classic_left",
                    title=title,
                    authors=authors,
                    subtitle_text=subtitle_text,
                    aff_logos=aff_logos,
                    has_conf=has_conf,
                    logo_scale=float(self.header_config.get("conservative_logo_scale", 0.82)),
                    fallback=True,
                )

            state["header_plan"] = plan
            state["current_agent"] = self.name
            self._save_outputs(state, plan)

            log_agent_success(self.name, f"planned header route: {plan['route']}")
            return state
        except Exception as exc:
            log_agent_error(self.name, f"failed: {exc}")
            state["errors"].append(f"{self.name}: {exc}")
            return state

    def _resolve_template_layout(self, state: PosterState) -> Dict[str, Any]:
        template_layout = state.get("layout_template_metadata") or {}
        if template_layout.get("header"):
            return template_layout

        requested_template = (
            state.get("resolved_layout_template")
            or state.get("layout_template")
            or "three_column_postergen"
        )
        if requested_template == "auto":
            requested_template = "three_column_postergen"

        poster_margin = float(self.config["layout"]["poster_margin"])
        column_spacing = float(self.config["layout"]["column_spacing"])
        effective_height = float(state["poster_height"]) - 2 * poster_margin
        header_height = effective_height * float(self.config["layout"]["title_height_fraction"])

        template_layout = LayoutTemplates(
            float(state["poster_width"]),
            float(state["poster_height"]),
            margin=poster_margin,
            col_gap=column_spacing,
        ).get_template(
            str(requested_template),
            header_height=header_height,
            width_ratios=state.get("adaptive_lane_widths"),
        )
        state["resolved_layout_template"] = template_layout["template_name"]
        state["layout_template_metadata"] = template_layout
        return template_layout

    def _title_and_authors(self, state: PosterState) -> Tuple[str, str]:
        narrative = state.get("narrative_content") or {}
        meta = narrative.get("meta", {})
        title = (
            meta.get("poster_title")
            or meta.get("title")
            or (state.get("story_board") or {}).get("title")
            or state.get("poster_name")
            or "Title"
        )
        authors = meta.get("authors") or "Authors"
        return (
            normalize_title_for_poster(str(title)) or "Title",
            normalize_text_for_poster(str(authors)) or "Authors",
        )

    def _collect_affiliation_logos(self, state: PosterState) -> List[Dict[str, Any]]:
        logos = [
            dict(logo)
            for logo in (state.get("affiliation_logos") or [])
            if logo.get("logo_path") and Path(str(logo["logo_path"])).exists()
        ]
        manual_logo = self._manual_affiliation_logo_entry(state)
        if manual_logo and not any(
            Path(str(logo["logo_path"])).resolve() == Path(str(manual_logo["logo_path"])).resolve()
            for logo in logos
        ):
            logos.insert(0, manual_logo)
        max_logos = int(self.config.get("affiliation_logos", {}).get("max_logos", 4))
        return logos[:max_logos]

    def _manual_affiliation_logo_entry(self, state: PosterState) -> Optional[Dict[str, Any]]:
        logo_path = state.get("aff_logo_path")
        if not logo_path or not Path(str(logo_path)).exists():
            return None
        return {
            "institution": state.get("affiliation_logo_label") or "Affiliation",
            "logo_path": str(logo_path),
            "domain": None,
            "source": "manual",
            "aspect": self._get_image_aspect_ratio(str(logo_path)),
        }

    def _rng(self, state: PosterState, title: str) -> random.Random:
        seed = state.get("header_seed")
        if seed is not None:
            return random.Random(str(seed))
        if self.header_config.get("stable_random_by_default", False):
            return random.Random(f"{state.get('poster_name', '')}:{title}:{int(time.time() // 86400)}")
        return random.Random()

    def _select_route(
        self,
        state: PosterState,
        template_layout: Dict[str, Any],
        has_conf: bool,
        aff_logos: List[Dict[str, Any]],
        rng: random.Random,
    ) -> str:
        requested = str(
            state.get("header_route")
            or self.header_config.get("default_route")
            or "auto"
        ).strip()
        if requested not in self.VALID_ROUTES:
            requested = "auto"

        allowed = [
            route
            for route in self.header_config.get("allowed_routes", ["classic_left", "centered", "right_title", "split_logos"])
            if route in self.VALID_ROUTES and route != "auto"
        ]
        if not allowed:
            allowed = ["classic_left", "centered", "right_title", "split_logos"]

        eligible = []
        for route in allowed:
            if route == "split_logos" and not (has_conf and aff_logos):
                continue
            if route == "right_title" and not (has_conf or aff_logos):
                eligible.append(route)
                continue
            eligible.append(route)

        if template_layout.get("orientation") == "portrait" and "split_logos" in eligible:
            eligible.remove("split_logos")
        if not eligible:
            eligible = ["classic_left"]

        if requested != "auto":
            return requested if requested in eligible else "classic_left"
        return rng.choice(eligible)

    def _select_subtitle(self, state: PosterState, title: str, rng: random.Random) -> str:
        policy = str(
            state.get("header_subtitle_policy")
            or self.header_config.get("subtitle_policy")
            or "auto"
        ).strip()
        if policy not in self.VALID_SUBTITLE_POLICIES:
            policy = "auto"
        if policy == "off":
            return ""

        words = re.findall(r"[A-Za-z0-9]+", title)
        short_by_chars = len(title) <= int(self.header_config.get("short_title_max_chars", 82))
        short_by_words = len(words) <= int(self.header_config.get("short_title_max_words", 11))
        if not (short_by_chars or short_by_words):
            return ""
        if policy == "auto" and rng.random() > float(self.header_config.get("subtitle_probability", 0.5)):
            return ""
        return self._generate_subtitle(state, title)

    def _generate_subtitle(self, state: PosterState, title: str) -> str:
        max_chars = int(self.header_config.get("subtitle_max_chars", 86))
        candidates = self._subtitle_candidates_from_state(state)
        for candidate in candidates:
            cleaned = self._clean_subtitle(candidate)
            if 18 <= len(cleaned) <= max_chars:
                return cleaned

        if re.search(r"\sfor\s", title, flags=re.IGNORECASE):
            prefix, suffix = re.split(r"\sfor\s", title, maxsplit=1, flags=re.IGNORECASE)
            candidate = f"Visualizing {prefix.strip()} for {suffix.strip()}"
        else:
            keywords = [
                word
                for word in re.findall(r"[A-Za-z][A-Za-z0-9-]+", title)
                if word.lower() not in {"the", "and", "with", "from", "using", "toward", "towards"}
            ][:6]
            phrase = " ".join(keywords) if keywords else "the paper's main idea"
            candidate = f"Motivation, method, and evidence for {phrase}"
        return self._shorten_subtitle(candidate, max_chars)

    def _subtitle_candidates_from_state(self, state: PosterState) -> List[str]:
        candidates: List[str] = []
        story_board = state.get("story_board") or {}
        for section in story_board.get("spatial_content_plan", {}).get("sections", []):
            role = str(section.get("content_role") or section.get("content_type") or "").lower()
            if role and not any(token in role for token in ("overview", "foundation", "motivation", "intro")):
                continue
            for text in section.get("text_content") or []:
                candidates.append(str(text))

        for item in state.get("paper_poster_keypoints") or []:
            for key in ("poster_text", "claim", "summary", "text"):
                if item.get(key):
                    candidates.append(str(item[key]))

        raw_text = str(state.get("raw_text") or "")
        abstract_match = re.search(r"(?is)\babstract\b\s*[:\-]?\s*(.{40,420}?)(?:\n\s*\b1\b|\n\s*introduction\b|\n\s*keywords\b)", raw_text)
        if abstract_match:
            candidates.append(abstract_match.group(1))
        return candidates

    def _clean_subtitle(self, text: str) -> str:
        text = normalize_text_for_poster(text)
        text = re.sub(r"^[\-\u2022\u25e6\*\s]+", "", text)
        text = re.sub(r"\[[^\]]+\]", "", text)
        text = re.sub(r"\([^)]{0,24}\d{2,4}[^)]{0,24}\)", "", text)
        text = re.sub(r"\s+", " ", text).strip(" .;:-")
        first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
        return self._shorten_subtitle(first_sentence or text, int(self.header_config.get("subtitle_max_chars", 86)))

    def _shorten_subtitle(self, text: str, max_chars: int) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= max_chars:
            return text
        truncated = text[: max_chars + 1].rsplit(" ", 1)[0].strip()
        return truncated.rstrip(".,;:") if truncated else text[:max_chars].rstrip(".,;:")

    def _build_plan(
        self,
        *,
        state: PosterState,
        template_layout: Dict[str, Any],
        route: str,
        title: str,
        authors: str,
        subtitle_text: str,
        aff_logos: List[Dict[str, Any]],
        has_conf: bool,
        logo_scale: float,
        fallback: bool,
    ) -> Dict[str, Any]:
        header = template_layout["header"]
        title_font_size, subtitle_font_size, author_font_size = self._font_sizes(template_layout, bool(subtitle_text))
        title_box, logo_regions, alignment = self._route_boxes(template_layout, route, has_conf, aff_logos)
        layout_mode = "split" if {"aff", "conf"}.issubset(set(logo_regions)) else "combined"
        logo_elements = self._logo_elements(
            state=state,
            logo_regions=logo_regions,
            layout_mode=layout_mode,
            has_conf=has_conf,
            aff_logos=aff_logos,
            logo_scale=logo_scale,
        )
        title_metrics = self._title_metrics(title_box["h"], title_font_size, subtitle_font_size, author_font_size, bool(subtitle_text))
        plan = {
            "selected_template": template_layout.get("template_name"),
            "route": route,
            "fallback": fallback,
            "logo_scale": logo_scale,
            "title_box": title_box,
            "title": {
                "text": title,
                "alignment": alignment,
                "font_size": title_font_size,
                "font_family": self.config["typography"]["fonts"].get("title", "Georgia"),
                "box_height": title_metrics["title_box_height"],
            },
            "subtitle": {
                "text": subtitle_text,
                "font_size": subtitle_font_size,
                "box_height": title_metrics["subtitle_box_height"],
                "top_gap_inches": title_metrics["title_to_subtitle_gap_inches"],
            },
            "authors": {
                "text": authors,
                "font_size": author_font_size,
                "box_height": title_metrics["author_box_height"],
                "top_gap_inches": title_metrics["author_top_gap_inches"],
            },
            "logo_regions": logo_regions,
            "logo_elements": logo_elements,
            "validation": {"passed": True, "reason": "ok"},
        }
        plan["validation"] = self._validate_plan(plan, header)
        return plan

    def _font_sizes(self, template_layout: Dict[str, Any], has_subtitle: bool) -> Tuple[int, int, int]:
        orientation = template_layout.get("orientation")
        template_name = template_layout.get("template_name")
        if orientation == "portrait":
            title_size = int(self.header_config.get("portrait_title_font_size", 58))
            author_size = int(self.header_config.get("portrait_author_font_size", 34))
        elif template_name == "cluster_72":
            title_size = int(self.header_config.get("large_landscape_title_font_size", 108))
            author_size = int(self.header_config.get("large_landscape_author_font_size", 50))
        else:
            title_size = int(self.header_config.get("landscape_title_font_size", 100))
            author_size = int(self.header_config.get("landscape_author_font_size", 72))
        if has_subtitle:
            title_size = int(title_size * float(self.header_config.get("subtitle_title_scale", 0.94)))
        subtitle_size = max(int(title_size * float(self.header_config.get("subtitle_font_scale", 0.58))), 24)
        return title_size, subtitle_size, author_size

    def _route_boxes(
        self,
        template_layout: Dict[str, Any],
        route: str,
        has_conf: bool,
        aff_logos: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]], str]:
        header = template_layout["header"]
        x0 = float(header["x"])
        y0 = float(header["y"])
        w = float(header["w"])
        h = float(header["h"])
        gap = float(self.header_config.get("title_logo_gap_inches", 0.46))
        vertical_pad = min(max(h * 0.10, 0.12), 0.35)
        title_h = max(h - 0.15, 0.8)
        has_logo = has_conf or bool(aff_logos)

        if not has_logo:
            return {"x": x0, "y": y0, "w": w, "h": title_h}, {}, self._alignment_for_route(route)

        if route in {"centered", "split_logos"} and has_conf and aff_logos:
            side_w = min(max(w * 0.17, 2.8), w * 0.24)
            logo_y = y0 + vertical_pad
            logo_h = max(h - 2 * vertical_pad, 0.65)
            left = {"x": x0, "y": logo_y, "w": side_w, "h": logo_h}
            right = {"x": x0 + w - side_w, "y": logo_y, "w": side_w, "h": logo_h}
            title_x = left["x"] + left["w"] + gap
            title_w = max(right["x"] - title_x - gap, w * 0.42)
            return {"x": title_x, "y": y0, "w": title_w, "h": title_h}, {"aff": left, "conf": right}, "center"

        explicit_logo = self._rightmost_logo_region(template_layout) if route != "right_title" else None
        reserve_frac = self._reserve_fraction(template_layout, has_conf, len(aff_logos))
        min_logo_w = 2.6 if template_layout.get("orientation") == "portrait" else 4.0
        max_logo_w = w * float(self.header_config.get("max_logo_zone_width_fraction", 0.38))
        logo_w = min(max(w * reserve_frac, min_logo_w), max_logo_w)
        logo_h = max(h - 2 * vertical_pad, 0.65)
        logo_y = y0 + vertical_pad

        if route == "right_title":
            logo_box = {"x": x0, "y": logo_y, "w": logo_w, "h": logo_h}
            title_x = logo_box["x"] + logo_box["w"] + gap
            title_w = max(x0 + w - title_x, w * 0.48)
            return {"x": title_x, "y": y0, "w": min(title_w, x0 + w - title_x), "h": title_h}, {"combined": logo_box}, "right"

        if explicit_logo:
            logo_box = explicit_logo
            title_w = max(logo_box["x"] - x0 - gap, w * 0.45)
            return {"x": x0, "y": y0, "w": min(title_w, w), "h": title_h}, {"combined": logo_box}, self._alignment_for_route(route)

        logo_box = {"x": x0 + w - logo_w, "y": logo_y, "w": logo_w, "h": logo_h}
        title_w = max(logo_box["x"] - x0 - gap, w * 0.50)
        return {"x": x0, "y": y0, "w": min(title_w, max(logo_box["x"] - x0 - gap, 0.1)), "h": title_h}, {"combined": logo_box}, self._alignment_for_route(route)

    def _alignment_for_route(self, route: str) -> str:
        if route == "centered":
            return "center"
        if route == "right_title":
            return "right"
        return "left"

    def _rightmost_logo_region(self, template_layout: Dict[str, Any]) -> Optional[Dict[str, float]]:
        logo_regions = template_layout.get("logo_regions") or []
        if not logo_regions:
            return None
        region = max(logo_regions, key=lambda item: item.get("x", 0))
        return {
            "x": float(region["x"]),
            "y": float(region["y"]),
            "w": float(region["w"]),
            "h": float(region["h"]),
        }

    def _reserve_fraction(self, template_layout: Dict[str, Any], has_conf: bool, aff_count: int) -> float:
        if template_layout.get("orientation") == "portrait":
            if has_conf and aff_count:
                return 0.34
            if aff_count >= 3:
                return 0.30
            return 0.24
        if has_conf and aff_count:
            return 0.36
        if aff_count >= 3:
            return 0.32
        return 0.26

    def _logo_elements(
        self,
        *,
        state: PosterState,
        logo_regions: Dict[str, Dict[str, float]],
        layout_mode: str,
        has_conf: bool,
        aff_logos: List[Dict[str, Any]],
        logo_scale: float,
    ) -> List[Dict[str, Any]]:
        if not logo_regions:
            return []
        if layout_mode == "split":
            elements: List[Dict[str, Any]] = []
            elements.extend(self._layout_aff_grid(aff_logos, logo_regions["aff"], logo_scale))
            if has_conf:
                elements.extend(self._layout_conf_only(str(state["logo_path"]), logo_regions["conf"], logo_scale))
            return elements

        region = logo_regions.get("combined")
        if not region:
            return []
        if has_conf and aff_logos:
            return self._layout_combined(str(state["logo_path"]), aff_logos, region, logo_scale)
        if has_conf:
            return self._layout_conf_only(str(state["logo_path"]), region, logo_scale)
        return self._layout_aff_grid(aff_logos, region, logo_scale)

    def _layout_conf_only(self, conf_path: str, region: Dict[str, float], scale: float) -> List[Dict[str, Any]]:
        aspect = self._get_image_aspect_ratio(conf_path)
        max_frac = float(self.header_config.get("max_logo_header_fraction", 0.78))
        logo_h = min(region["h"] * max_frac * scale, region["w"] / max(aspect, 0.1))
        logo_w = logo_h * aspect
        return [{
            "type": "conf_logo",
            "x": region["x"] + (region["w"] - logo_w) / 2,
            "y": region["y"] + (region["h"] - logo_h) / 2,
            "width": logo_w,
            "height": logo_h,
            "priority": 0.9,
            "header_planned": True,
        }]

    def _layout_combined(
        self,
        conf_path: str,
        aff_logos: List[Dict[str, Any]],
        region: Dict[str, float],
        scale: float,
    ) -> List[Dict[str, Any]]:
        conf_cfg = self.config.get("conference_logos", {})
        divider_w = float(conf_cfg.get("divider_width", 0.04))
        gap = float(conf_cfg.get("divider_gap", 0.22))
        conf_frac = float(conf_cfg.get("conf_zone_fraction", 0.48))
        conf_zone_w = region["w"] * conf_frac
        aff_zone_w = max(region["w"] - conf_zone_w - divider_w - 2 * gap, 0.2)
        aff_region = {"x": region["x"], "y": region["y"], "w": aff_zone_w, "h": region["h"]}
        conf_region = {
            "x": region["x"] + aff_zone_w + divider_w + 2 * gap,
            "y": region["y"],
            "w": conf_zone_w,
            "h": region["h"],
        }
        elements = self._layout_aff_grid(aff_logos, aff_region, scale)
        elements.append({
            "type": "logo_divider",
            "x": region["x"] + aff_zone_w + gap,
            "y": region["y"] + region["h"] * 0.08,
            "width": divider_w,
            "height": region["h"] * 0.84,
            "priority": 0.85,
            "header_planned": True,
        })
        elements.extend(self._layout_conf_only(conf_path, conf_region, scale))
        return elements

    def _layout_aff_grid(
        self,
        aff_logos: List[Dict[str, Any]],
        region: Dict[str, float],
        scale: float,
    ) -> List[Dict[str, Any]]:
        count = len(aff_logos)
        if count == 0:
            return []
        if count == 1:
            cols, rows = 1, 1
        elif count == 2:
            cols, rows = 2, 1
        elif count == 3:
            cols, rows = 3, 1
        elif count == 4:
            cols, rows = 2, 2
        else:
            cols, rows = 3, 2

        logo_cfg = self.config.get("affiliation_logos", {})
        gap = float(logo_cfg.get("logo_box_gap", 0.24))
        cell_w = max((region["w"] - (cols - 1) * gap) / cols, 0.2)
        cell_h = max((region["h"] - (rows - 1) * gap) / rows, 0.2)
        max_h = min(
            float(logo_cfg.get("max_logo_height", 1.55)),
            region["h"] * float(self.header_config.get("max_logo_header_fraction", 0.78)),
        ) * scale
        cell_h = min(cell_h, max_h)
        grid_h = rows * cell_h + (rows - 1) * gap
        grid_w = cols * cell_w + (cols - 1) * gap
        start_y = region["y"] + max((region["h"] - grid_h) / 2, 0)
        start_x = region["x"] + max((region["w"] - grid_w) / 2, 0)

        elements: List[Dict[str, Any]] = []
        for index, logo in enumerate(aff_logos):
            row, col = divmod(index, cols)
            elements.append({
                "type": "institution_logo",
                "x": start_x + col * (cell_w + gap),
                "y": start_y + row * (cell_h + gap),
                "width": cell_w,
                "height": cell_h,
                "image_path": logo["logo_path"],
                "institution": logo.get("institution", ""),
                "domain": logo.get("domain"),
                "source": logo.get("source"),
                "aspect": logo.get("aspect", self._get_image_aspect_ratio(logo.get("logo_path"))),
                "priority": 0.9,
                "header_planned": True,
            })
        return elements

    def _title_metrics(
        self,
        box_height: float,
        title_font_size: int,
        subtitle_font_size: int,
        author_font_size: int,
        has_subtitle: bool,
    ) -> Dict[str, float]:
        author_gap = float(self.config["typography"].get("title_author_gap_points", 16)) / 72
        subtitle_gap = float(self.header_config.get("title_subtitle_gap_inches", 0.08)) if has_subtitle else 0.0
        author_box_h = min(max((author_font_size / 72) * 1.12, 0.45), max(box_height * 0.30, 0.45))
        subtitle_box_h = max((subtitle_font_size / 72) * 1.12, 0.36) if has_subtitle else 0.0
        title_box_h = max(box_height - subtitle_gap - subtitle_box_h - author_gap - author_box_h, box_height * 0.46)
        if title_box_h + subtitle_gap + subtitle_box_h + author_gap + author_box_h > box_height:
            title_box_h = max(box_height - subtitle_gap - subtitle_box_h - author_gap - author_box_h, box_height * 0.38)
        return {
            "title_box_height": title_box_h,
            "subtitle_box_height": subtitle_box_h,
            "title_to_subtitle_gap_inches": subtitle_gap,
            "author_top_gap_inches": author_gap,
            "author_box_height": author_box_h,
        }

    def _validate_plan(self, plan: Dict[str, Any], header: Dict[str, float]) -> Dict[str, Any]:
        title_box = self._box_from_wh(plan["title_box"])
        header_box = self._box_from_header(header)
        min_gap = float(self.header_config.get("min_title_logo_gap_inches", 0.20))
        if title_box[2] <= title_box[0] or title_box[3] <= title_box[1]:
            return {"passed": False, "reason": "invalid_title_box"}
        if not self._contains(header_box, title_box, tolerance=0.08):
            return {"passed": False, "reason": "title_outside_header"}
        min_title_w = float(header["w"]) * float(self.header_config.get("min_title_width_fraction", 0.42))
        if plan["title_box"]["w"] < min_title_w:
            return {"passed": False, "reason": "title_box_too_narrow"}

        padded_title = (title_box[0] - min_gap, title_box[1] - 0.02, title_box[2] + min_gap, title_box[3] + 0.02)
        max_logo_h = float(header["h"]) * float(self.header_config.get("max_logo_header_fraction", 0.78)) * 1.05
        for element in plan.get("logo_elements") or []:
            if element.get("type") == "logo_divider":
                continue
            box = self._box_from_wh(element)
            if not self._contains(header_box, box, tolerance=0.10):
                return {"passed": False, "reason": f"{element.get('type')}_outside_header"}
            if self._intersects(padded_title, box):
                return {"passed": False, "reason": f"{element.get('type')}_overlaps_title"}
            if float(element.get("height", 0.0)) > max_logo_h:
                return {"passed": False, "reason": f"{element.get('type')}_too_tall"}
        return {"passed": True, "reason": "ok"}

    def _box_from_header(self, box: Dict[str, float]) -> Tuple[float, float, float, float]:
        return (float(box["x"]), float(box["y"]), float(box["x"]) + float(box["w"]), float(box["y"]) + float(box["h"]))

    def _box_from_wh(self, box: Dict[str, float]) -> Tuple[float, float, float, float]:
        return (
            float(box["x"]),
            float(box["y"]),
            float(box["x"]) + float(box.get("w", box.get("width", 0.0))),
            float(box["y"]) + float(box.get("h", box.get("height", 0.0))),
        )

    def _contains(
        self,
        outer: Tuple[float, float, float, float],
        inner: Tuple[float, float, float, float],
        *,
        tolerance: float = 0.0,
    ) -> bool:
        return (
            inner[0] >= outer[0] - tolerance
            and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance
            and inner[3] <= outer[3] + tolerance
        )

    def _intersects(
        self,
        a: Tuple[float, float, float, float],
        b: Tuple[float, float, float, float],
    ) -> bool:
        return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

    def _get_image_aspect_ratio(self, image_path: str | None) -> float:
        if not image_path or not Path(str(image_path)).exists():
            return float(self.config["layout_constants"]["default_logo_aspect_ratio"])
        from PIL import Image

        with Image.open(str(image_path)) as image:
            return image.size[0] / max(image.size[1], 1)

    def _save_outputs(self, state: PosterState, plan: Dict[str, Any]) -> None:
        output_dir = Path(state["output_dir"]) / "content"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "header_plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def header_planner_node(state: PosterState) -> Dict[str, Any]:
    result = HeaderPlanner()(state)
    return {
        **state,
        "header_plan": result.get("header_plan"),
        "resolved_layout_template": result.get("resolved_layout_template"),
        "layout_template_metadata": result.get("layout_template_metadata"),
        "current_agent": result.get("current_agent"),
        "errors": result.get("errors"),
    }
