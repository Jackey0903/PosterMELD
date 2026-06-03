from pathlib import Path

from PIL import Image

from src.agents.affiliation_logo_agent import AffiliationLogoAgent
from src.agents.adaptive_column_relayout import AdaptiveColumnRelayoutAgent
from src.agents.font_agent import FontAgent
from src.agents.layout_agent import LayoutAgent
from src.agents.micro_layout_refiner import MicroLayoutRefiner
from src.agents.parser import Parser
from src.agents.renderer import Renderer
from src.agents.template_block_planner import TemplateBlockPlanner
from src.agents.vlm_layout_reviewer import VLMLayoutReviewer
from src.agents.visual_asset_agent import VisualAssetAgent
from src.agents.visual_legibility_reviewer import VisualLegibilityReviewer
from src.config.poster_config import load_config
from src.layout.template_selector import TemplateSelector
from src.state.poster_state import create_state, _get_model_config
from src.template_extraction.block_template_registry import (
    get_block_template_info,
    list_block_template_ids,
    load_block_template_layout,
)
from src.template_extraction.extract_templates import build_template
from src.template_extraction.registry import list_extracted_template_ids, load_extracted_template
from src.tools.layout_api import LayoutTemplates
from src.utils.text_cleanup import normalize_text_for_poster


def test_parser_visual_assets_registry_matches_images_tables():
    parser = Parser.__new__(Parser)
    figures = {
        "1": {
            "caption": "Figure 1",
            "path": "/tmp/figure-1.png",
            "aspect": 1.5,
        }
    }
    tables = {
        "2": {
            "caption": "Table 2",
            "path": "/tmp/table-2.png",
            "aspect": 2.0,
        }
    }

    visual_assets = parser._build_visual_registry(figures, tables)

    assert visual_assets["figure_1"]["source_path"] == figures["1"]["path"]
    assert visual_assets["figure_1"]["asset_type"] == "figure"
    assert visual_assets["table_2"]["source_path"] == tables["2"]["path"]
    assert visual_assets["table_2"]["asset_type"] == "table"


def test_parser_extracts_affiliations_from_paper_header():
    parser = Parser.__new__(Parser)
    raw_text = """
    # Example Paper
    A. One, B. Two
    Department of Computer Science and Engineering, Washington University in St. Louis, USA
    Department of Computer Science and Engineering, George Mason University
    Brown School at Washington University in St. Louis, USA

    #### Abstract
    Body starts here.
    """

    affiliations = parser._extract_affiliations(raw_text)

    assert "Washington University in St. Louis" in affiliations
    assert "George Mason University" in affiliations
    assert "Brown School at Washington University in St. Louis" in affiliations


def test_parser_does_not_extract_reference_doi_as_paper_doi():
    parser = Parser.__new__(Parser)
    raw_text = """
    # Can Watermarked LLMs Be Identified By Users Via Crafted Prompts?
    Tsinghua University

    A BSTRACT
    Body starts here.

    References
    Some unrelated paper. DOI: 10.1145/3626772.3661377
    """

    assert parser._extract_doi(raw_text) is None


def test_parser_extracts_watermark_paper_affiliations_from_header():
    parser = Parser.__new__(Parser)
    raw_text = """
    # Can Watermarked LLMs Be Identified By Users Via Crafted Prompts?
    Tsinghua University
    Beijing University of Posts and Telecommunications
    The Chinese University of Hongkong
    University of Illinois at Chicago
    Hongkong University of Science and Technology (Guangzhou)

    A BSTRACT
    Body starts here.
    """

    affiliations = parser._extract_affiliations(raw_text)

    assert "Tsinghua University" in affiliations
    assert "Beijing University of Posts and Telecommunications" in affiliations
    assert "The Chinese University of Hong Kong" in affiliations
    assert "University of Illinois at Chicago" in affiliations
    assert "Hong Kong University of Science and Technology (Guangzhou)" in affiliations


def test_affiliation_logo_agent_creates_placeholder_when_download_fails(tmp_path, monkeypatch):
    state = create_state(str(tmp_path / "paper.pdf"), enable_affiliation_logos=True)
    state["output_dir"] = str(tmp_path / "output")
    state["affiliations"] = ["Example Research University"]

    agent = AffiliationLogoAgent()
    agent.config["include_placeholders"] = True
    monkeypatch.setattr(agent, "_download_clearbit_logo", lambda domain, output_path: None)
    monkeypatch.setattr(agent, "_download_wikidata_logo", lambda institution, output_path: None)
    monkeypatch.setattr(agent, "_download_known_commons_logo", lambda institution, output_path: None)

    result = agent(state)

    logos = result["affiliation_logos"]
    assert len(logos) == 1
    assert logos[0]["status"] == "placeholder"
    assert Path(logos[0]["logo_path"]).exists()
    assert (Path(state["output_dir"]) / "content" / "affiliation_logos.json").exists()


def test_layout_agent_places_affiliation_logos_in_title_right_region(tmp_path):
    logo_paths = []
    for index in range(3):
        path = tmp_path / f"logo_{index}.png"
        Image.new("RGBA", (300, 120), (255, 255, 255, 255)).save(path)
        logo_paths.append(str(path))

    state = create_state("/tmp/paper.pdf", enable_affiliation_logos=True)
    state["affiliation_logos"] = [
        {
            "institution": f"Institution {index}",
            "logo_path": path,
            "domain": None,
            "source": "test",
            "aspect": 2.5,
        }
        for index, path in enumerate(logo_paths)
    ]

    agent = LayoutAgent()
    template = agent._resolve_template_layout(state)
    elements = agent._create_logo_elements(state, state["poster_width"], template)

    assert len(elements) == 3
    assert {element["type"] for element in elements} == {"institution_logo"}
    title = agent._create_title_element(state, state["poster_width"], template["header"]["h"], template)
    assert all(element["x"] >= title["x"] + title["width"] for element in elements)


def test_layout_agent_avoids_title_conference_logo_overlap_for_portrait_templates(tmp_path):
    logo_path = tmp_path / "conference.png"
    Image.new("RGBA", (900, 420), (20, 80, 160, 255)).save(logo_path)

    agent = LayoutAgent()
    for template_id in list_block_template_ids():
        info = get_block_template_info(template_id)
        canvas = info["recommended_canvas_size"]
        state = create_state("/tmp/paper.pdf", layout_template=template_id)
        state["poster_width"] = canvas["width"]
        state["poster_height"] = canvas["height"]
        state["resolved_layout_template"] = template_id
        state["template_layout_mode"] = "template_prior"
        state["logo_path"] = str(logo_path)

        template = agent._resolve_template_layout(state)
        title = agent._create_title_element(state, state["poster_width"], template["header"]["h"], template)
        logo = next(
            element for element in agent._create_logo_elements(state, state["poster_width"], template)
            if element["type"] == "conf_logo"
        )

        title_right = title["x"] + title["width"]
        logo_left = logo["x"]
        assert title_right <= logo_left


def test_layout_agent_supports_five_affiliation_logos_without_overflow(tmp_path):
    logo_paths = []
    for index in range(5):
        path = tmp_path / f"logo_{index}.png"
        Image.new("RGBA", (420, 120), (20, 50, 120, 255)).save(path)
        logo_paths.append(str(path))

    state = create_state("/tmp/paper.pdf", enable_affiliation_logos=True)
    state["affiliation_logos"] = [
        {
            "institution": f"Institution {index}",
            "logo_path": path,
            "domain": None,
            "source": "test",
            "aspect": 3.5,
        }
        for index, path in enumerate(logo_paths)
    ]

    agent = LayoutAgent()
    template = agent._resolve_template_layout(state)
    elements = agent._create_logo_elements(state, state["poster_width"], template)
    logos = [element for element in elements if element.get("type") == "institution_logo"]
    region = agent._title_logo_region(template, False)

    assert len(logos) == 5
    assert all(region["x"] <= logo["x"] for logo in logos)
    assert all(logo["x"] + logo["width"] <= region["x"] + region["w"] + 1e-6 for logo in logos)
    assert all(region["y"] <= logo["y"] for logo in logos)
    assert all(logo["y"] + logo["height"] <= region["y"] + region["h"] + 1e-6 for logo in logos)


def test_micro_layout_refiner_keeps_logo_divider_as_global_element():
    state = create_state("/tmp/paper.pdf", layout_template="three_column_postergen")
    state["styled_layout"] = [
        {
            "type": "title",
            "x": 1.0,
            "y": 1.0,
            "width": 32.0,
            "height": 4.0,
            "priority": 1.0,
        },
        {
            "type": "logo_divider",
            "x": 42.0,
            "y": 1.5,
            "width": 0.04,
            "height": 3.5,
            "priority": 0.85,
        },
        {
            "type": "section_container",
            "section_id": "left::s1",
            "lane_id": "left",
            "x": 1.0,
            "y": 7.0,
            "width": 16.0,
            "height": 4.0,
            "priority": 0.1,
        },
        {
            "type": "text",
            "id": "left::s1_text",
            "section_id": "left::s1",
            "x": 1.3,
            "y": 7.6,
            "width": 15.4,
            "height": 2.5,
            "content": "Short text",
            "font_size": 40,
            "priority": 0.5,
        },
    ]

    result = MicroLayoutRefiner()(state)
    divider = next(element for element in result["styled_layout"] if element.get("type") == "logo_divider")

    assert divider["y"] == 1.5


def test_renderer_uses_element_path_for_institution_logo(tmp_path):
    logo_path = tmp_path / "logo.png"
    Image.new("RGBA", (100, 80), (255, 255, 255, 255)).save(logo_path)

    calls = []

    class FakeDirector:
        def add_image(self, *args, **kwargs):
            calls.append((args, kwargs))

    renderer = Renderer()
    renderer.director = FakeDirector()
    element = {
        "type": "institution_logo",
        "x": 1,
        "y": 1,
        "width": 2,
        "height": 1,
        "image_path": str(logo_path),
    }

    renderer._render_institution_logo(None, element, create_state("/tmp/paper.pdf"))

    assert calls
    assert calls[0][0][0] == str(logo_path)


def test_block_template_registry_exposes_cluster_templates():
    template_ids = set(list_block_template_ids())

    assert {"cluster_0", "cluster_1", "cluster_2", "cluster_3"}.issubset(template_ids)


def test_block_template_layout_identifies_header_and_content_slots():
    layout = load_block_template_layout("cluster_2", 36, 51, margin=1.0)

    assert layout["layout_mode"] == "template_prior"
    assert layout["orientation"] == "portrait"
    assert layout["template_aspect_ratio"] < 1
    assert layout["header_slot"]["slot_id"] == "slot_0"
    assert len(layout["content_slots"]) == 4
    assert len(layout["lanes"]) == 4
    assert all(slot["x"] + slot["w"] <= 36.05 for slot in layout["content_slots"])
    assert all(slot["y"] + slot["h"] <= 51.05 for slot in layout["content_slots"])
    assert all(slot["slot_id"] != layout["header_slot"]["slot_id"] for slot in layout["content_slots"])


def test_template_block_planner_matches_block_count_to_content_slots(monkeypatch):
    json_response = """{
      "blocks": [
        {"target_title": "Overview", "target_bullets": ["Problem framing.", "Core idea."]},
        {"target_title": "Method", "target_bullets": ["Framework details.", "Optimization flow."]},
        {"target_title": "Results", "target_bullets": ["Main result.", "Comparison summary."]},
        {"target_title": "Takeaways", "target_bullets": ["Deployment note.", "Conclusion."]}
      ]
    }"""

    class FakeResponse:
        input_tokens = 1
        output_tokens = 1
        content = json_response

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def step(self, message):
            return FakeResponse()

    monkeypatch.setattr("src.agents.template_block_planner.LangGraphAgent", FakeAgent)

    state = create_state("/tmp/paper.pdf", layout_template="cluster_2")
    state["resolved_layout_template"] = "cluster_2"
    state["story_board"] = {
        "spatial_content_plan": {
            "sections": [
                {
                    "section_id": "overview",
                    "section_title": "Overview",
                    "column_assignment": "left",
                    "vertical_priority": "top",
                    "text_content": ["Problem framing.", "Core idea."],
                    "visual_assets": [],
                },
                {
                    "section_id": "method",
                    "section_title": "Method",
                    "column_assignment": "middle",
                    "vertical_priority": "top",
                    "text_content": ["Framework details.", "Optimization flow."],
                    "visual_assets": [{"visual_id": "figure_1"}],
                },
                {
                    "section_id": "results",
                    "section_title": "Results",
                    "column_assignment": "right",
                    "vertical_priority": "middle",
                    "text_content": ["Main result.", "Comparison summary."],
                    "visual_assets": [],
                },
            ]
        }
    }

    result = TemplateBlockPlanner()(state)

    blocks = result["template_block_plan"]["blocks"]
    assert len(blocks) == 3
    assert len({block["slot_id"] for block in blocks}) == 3
    rewritten_sections = result["story_board"]["spatial_content_plan"]["sections"]
    assert len(rewritten_sections) == 3
    assert all(section["column_assignment"].startswith("slot_") for section in rewritten_sections)
    assert all(section["slot_id"] == section["column_assignment"] for section in rewritten_sections)


def test_layout_templates_support_block_template_ids():
    template_names = LayoutTemplates.available_template_names()

    assert "cluster_0" in template_names
    info = get_block_template_info("cluster_0")
    assert info["orientation"] == "portrait"
    layout = LayoutTemplates(36, 51, margin=1.0, col_gap=1.0).get_template("cluster_0")
    assert layout["layout_mode"] == "template_prior"
    assert layout["orientation"] == "portrait"
    assert [lane["id"] for lane in layout["lanes"]][:2] == ["slot_1", "slot_2"]
    assert "base_layout_template" not in layout


def test_font_agent_keyword_prompt_uses_narrative_content(monkeypatch):
    captured = {}

    class FakeResponse:
        content = '{"section_keywords": {}, "formatting_summary": {}}'
        input_tokens = 1
        output_tokens = 1

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def step(self, message):
            captured["message"] = message
            return FakeResponse()

    state = create_state("/tmp/paper.pdf")
    state["narrative_content"] = {"and": "poster narrative signal"}
    agent = FontAgent()
    monkeypatch.setattr("src.agents.font_agent.LangGraphAgent", FakeAgent)

    agent._identify_keywords({"spatial_content_plan": {"sections": []}}, state)

    assert "poster narrative signal" in captured["message"]


def test_text_cleanup_repairs_mojibake_bullets_and_common_ocr_typos():
    text = "â¢ **Realistic Setting: When costs matter.\\nâ¦ Effcient search improves 42%â70% with î»L_BCE."

    cleaned = normalize_text_for_poster(text)

    assert "â" not in cleaned
    assert "• **Realistic Setting:** When costs matter." in cleaned
    assert "◦ Efficient search improves 42%-70% with lambda L_BCE." in cleaned


def test_renderer_resolves_figure_and_table_paths():
    renderer = Renderer()
    state = create_state("/tmp/paper.pdf")
    state["resolved_visual_assets"] = {
        "methods_figure_1": {"resolved_path": "/tmp/figure-slot.png"},
        "results_table_1": {"resolved_path": "/tmp/table-slot.png"},
    }

    assert renderer._get_resolved_visual_entry("methods_figure_1", "figure_1", state)["resolved_path"] == "/tmp/figure-slot.png"
    assert renderer._get_resolved_visual_entry("results_table_1", "table_1", state)["resolved_path"] == "/tmp/table-slot.png"


def test_visual_asset_agent_disabled_is_slot_preserving_crop_only(tmp_path):
    source_path = tmp_path / "source.png"
    from PIL import Image

    Image.new("RGB", (100, 80), color=(255, 0, 0)).save(source_path)

    state = create_state(str(tmp_path / "paper.pdf"))
    state["output_dir"] = str(tmp_path / "output")
    state["enable_visual_refinement"] = False
    state["visual_assets"] = {
        "figure_1": {
            "asset_id": "figure_1",
            "asset_type": "figure",
            "source_path": str(source_path),
            "resolved_path": None,
            "caption": "Figure 1",
            "aspect": 1.25,
            "provenance": "paper_extracted",
        }
    }
    state["styled_layout"] = [
        {
            "type": "visual",
            "slot_id": "method_figure_1",
            "id": "method_figure_1",
            "visual_id": "figure_1",
            "width": 2.0,
            "height": 1.0,
        }
    ]

    result = VisualAssetAgent()(state)

    assert result["visual_plan"][0]["action"] == "crop_only"
    assert "method_figure_1" in result["resolved_visual_assets"]
    assert Path(result["resolved_visual_assets"]["method_figure_1"]["resolved_path"]).exists()


def test_visual_asset_agent_enabled_generates_for_missing_source_slot(tmp_path):
    state = create_state(str(tmp_path / "paper.pdf"), enable_visual_refinement=True)
    state["output_dir"] = str(tmp_path / "output")
    state["visual_assets"] = {}
    state["styled_layout"] = [
        {
            "type": "visual",
            "slot_id": "method_generated_visual",
            "id": "method_generated_visual",
            "width": 2.0,
            "height": 1.0,
        }
    ]

    result = VisualAssetAgent()(state)

    assert result["visual_plan"][0]["action"] == "generate_new"
    resolved = result["resolved_visual_assets"]["method_generated_visual"]
    assert resolved["asset_type"] == "generated"
    assert Path(resolved["resolved_path"]).exists()


def test_vlm_layout_reviewer_disabled_is_noop():
    state = create_state("/tmp/paper.pdf")
    state["styled_layout"] = [{"type": "title", "id": "title", "x": 1, "y": 1, "width": 10, "height": 2}]

    result = VLMLayoutReviewer()(state)

    assert result.get("vlm_layout_review") is None
    assert result["styled_layout"] == state["styled_layout"]


def test_vlm_layout_reviewer_uses_responses_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "output_text": '{"overall_score": 90, "accept": true, "issues": [], "patch": [], "visual_asset_recommendations": []}'
            }

    def fake_post(url, headers, json, timeout, stream=False):
        captured["url"] = url
        captured["payload"] = json
        captured["stream"] = stream
        return FakeResponse()

    monkeypatch.setattr("src.agents.vlm_layout_reviewer.requests.post", fake_post)
    reviewer = VLMLayoutReviewer()
    response = reviewer._post_vlm_request(
        "https://example.com/api/v1/responses",
        {"Authorization": "Bearer test"},
        "gpt-5.4",
        "review",
        "data:image/png;base64,abc",
    )
    text = reviewer._extract_response_text(response)

    assert captured["url"] == "https://example.com/api/v1/responses"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["stream"] is True
    assert captured["stream"] is True
    assert captured["payload"]["input"][0]["content"][0]["type"] == "input_text"
    assert captured["payload"]["input"][0]["content"][1]["type"] == "input_image"
    assert "overall_score" in text


def test_gpt_54_uses_openai_chat_provider():
    config = _get_model_config("gpt-5.4")

    assert config.provider == "openai"
    assert config.model_name == "gpt-5.4"


def test_vlm_layout_reviewer_applies_single_safe_patch(tmp_path, monkeypatch):
    state = create_state(str(tmp_path / "paper.pdf"), enable_vlm_layout_review=True)
    state["output_dir"] = str(tmp_path / "output")
    Path(state["output_dir"], "content").mkdir(parents=True)
    preview = tmp_path / "preview.png"
    from PIL import Image

    Image.new("RGB", (200, 120), color=(255, 255, 255)).save(preview)
    state["poster_preview_path"] = str(preview)
    state["layout_template_metadata"] = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "three_column_postergen",
        header_height=(36 - 2) * 0.18,
    )
    left_lane = state["layout_template_metadata"]["lanes"][0]
    state["styled_layout"] = [
        {
            "type": "section_container",
            "section_id": "intro",
            "lane_id": "left",
            "x": left_lane["x"],
            "y": left_lane["y"],
            "width": left_lane["w"],
            "height": 4.0,
            "priority": 0.1,
        },
        {
            "type": "visual",
            "id": "intro_visual",
            "slot_id": "intro_visual",
            "visual_id": "figure_1",
            "x": left_lane["x"] + 1.0,
            "y": left_lane["y"] + 1.0,
            "width": 4.0,
            "height": 2.0,
            "priority": 0.4,
        },
    ]

    def fake_review(self, state):
        return {
            "overall_score": 82,
            "accept": False,
            "issues": [{"severity": "medium", "category": "whitespace", "target": "intro_visual"}],
            "patch": [{"target": "intro_visual", "op": "increase_visual_scale", "value": 1.1}],
            "visual_asset_recommendations": [],
        }

    monkeypatch.setattr(VLMLayoutReviewer, "_review_or_fallback", fake_review)
    result = VLMLayoutReviewer()(state)

    visual = next(element for element in result["styled_layout"] if element.get("id") == "intro_visual")
    assert result["vlm_patch_applied"] is True
    assert result["vlm_reflow_required"] is True
    assert result["vlm_review_count"] == 1
    assert visual["width"] > 4.0


def test_layout_templates_expose_multiple_geometry_families():
    templates = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0)

    three_col = templates.get_template("three_column_postergen", header_height=6.0)
    two_plus_one = templates.get_template("two_plus_one_mixed", header_height=6.0)
    one_plus_two = templates.get_template("one_plus_two_mixed", header_height=6.0)
    single_col = templates.get_template("single_column_vertical", header_height=6.0)
    adaptive = templates.get_template(
        "adaptive_three_column",
        header_height=6.0,
        width_ratios={"left": 0.85, "middle": 1.30, "right": 0.85},
    )

    assert len(three_col["lanes"]) == 3
    assert round(three_col["lanes"][0]["w"], 4) == round(three_col["lanes"][1]["w"], 4)

    assert two_plus_one["lanes"][2]["w"] > two_plus_one["lanes"][0]["w"]
    assert one_plus_two["lanes"][0]["w"] > one_plus_two["lanes"][1]["w"]

    assert len({round(lane["x"], 4) for lane in single_col["lanes"]}) == 1
    assert single_col["lanes"][0]["y"] < single_col["lanes"][1]["y"] < single_col["lanes"][2]["y"]

    assert adaptive["template_name"] == "adaptive_three_column"
    assert adaptive["lanes"][1]["w"] > adaptive["lanes"][0]["w"]
    assert adaptive["lanes"][1]["w"] > adaptive["lanes"][2]["w"]


def test_template_extractor_builds_expected_template_schema():
    image_path = Path("template/poster(1).png")
    if not image_path.exists():
        return

    template, raw = build_template(image_path)

    assert template["geometry_policy"] == "soft"
    assert template["source_lanes"]
    assert template["panel_style_tokens"]
    assert len(template["lanes"]) == 3
    assert raw["ocr_blocks"]
    for box in [template["header"], *template["lanes"], *template["logo_regions"]]:
        assert 0 <= box["x"] <= 1
        assert 0 <= box["y"] <= 1
        assert 0 < box["w"] <= 1
        assert 0 < box["h"] <= 1


def test_extracted_template_registry_and_scaling():
    template_ids = set(list_extracted_template_ids())
    if not template_ids:
        return

    assert "extracted_poster1_landscape_three_panel" in template_ids
    assert "extracted_poster2_landscape_multi_panel" in template_ids
    assert "extracted_poster3_portrait_section_band" in template_ids

    loaded = load_extracted_template("extracted_poster3_portrait_section_band")
    assert loaded is not None
    assert loaded["orientation"] == "portrait"

    layout = LayoutTemplates(36, 54, margin=1.0, col_gap=1.0).get_template(
        "extracted_poster3_portrait_section_band"
    )
    assert layout["template_name"] == "extracted_poster3_portrait_section_band"
    assert layout["orientation"] == "portrait"
    assert layout["geometry_policy"] == "soft"
    assert len(layout["lanes"]) == 3
    assert layout["lanes"][0]["w"] > layout["lanes"][0]["h"]
    assert layout["lanes"][0]["y"] < layout["lanes"][1]["y"] < layout["lanes"][2]["y"]
    assert layout["source_lanes"][0]["h"] != layout["lanes"][0]["h"]

    horizontal = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "extracted_poster1_landscape_three_panel",
        header_height=6.0,
    )
    body_height = 36 - 1.0 - horizontal["lanes"][0]["y"]
    assert len(horizontal["lanes"]) == 3
    assert horizontal["lanes"][0]["h"] >= body_height * 0.95
    assert horizontal["lanes"][0]["y"] < horizontal["source_lanes"][0]["y"]


def test_layout_agent_resolves_extracted_template_metadata():
    if "extracted_poster3_portrait_section_band" not in set(list_extracted_template_ids()):
        return

    state = create_state(
        "/tmp/paper.pdf",
        width=36,
        height=54,
        layout_template="extracted_poster3_portrait_section_band",
    )

    template = LayoutAgent()._resolve_template_layout(state)

    assert template["template_name"] == "extracted_poster3_portrait_section_band"
    assert state["resolved_layout_template"] == "extracted_poster3_portrait_section_band"
    assert template["header"]["w"] == 36 * 0.95
    assert template["lanes"][0]["w"] == 34


def test_layout_agent_respects_requested_template_geometry():
    state = create_state("/tmp/paper.pdf", layout_template="one_plus_two_mixed")
    state["narrative_content"] = {"meta": {"poster_title": "Paper", "authors": "Authors"}}
    state["story_board"] = {
        "spatial_content_plan": {
            "sections": [
                {
                    "section_id": "intro",
                    "section_title": "Intro",
                    "column_assignment": "left",
                    "vertical_priority": "top",
                    "text_content": ["Point A", "Point B"],
                    "visual_assets": [],
                    "importance_level": 2,
                },
                {
                    "section_id": "method",
                    "section_title": "Method",
                    "column_assignment": "middle",
                    "vertical_priority": "top",
                    "text_content": ["Point A", "Point B"],
                    "visual_assets": [],
                    "importance_level": 1,
                },
                {
                    "section_id": "results",
                    "section_title": "Results",
                    "column_assignment": "right",
                    "vertical_priority": "top",
                    "text_content": ["Point A", "Point B"],
                    "visual_assets": [],
                    "importance_level": 2,
                },
            ]
        }
    }

    result = LayoutAgent()(state, mode="initial")
    sections = {
        element["section_id"]: element
        for element in result["initial_layout_data"]
        if element.get("type") == "section_container"
    }

    assert sections["intro"]["width"] > sections["method"]["width"]
    assert sections["intro"]["width"] > sections["results"]["width"]


def test_layout_agent_respects_adaptive_lane_widths():
    state = create_state("/tmp/paper.pdf", layout_template="three_column_postergen")
    state["adaptive_lane_widths"] = {"left": 0.85, "middle": 1.30, "right": 0.85}
    state["narrative_content"] = {"meta": {"poster_title": "Paper", "authors": "Authors"}}
    state["story_board"] = {
        "spatial_content_plan": {
            "sections": [
                {
                    "section_id": "intro",
                    "section_title": "Intro",
                    "column_assignment": "left",
                    "vertical_priority": "top",
                    "text_content": ["Point A"],
                    "visual_assets": [],
                    "importance_level": 2,
                },
                {
                    "section_id": "method",
                    "section_title": "Method",
                    "column_assignment": "middle",
                    "vertical_priority": "top",
                    "text_content": ["Point A"],
                    "visual_assets": [],
                    "importance_level": 1,
                },
                {
                    "section_id": "results",
                    "section_title": "Results",
                    "column_assignment": "right",
                    "vertical_priority": "top",
                    "text_content": ["Point A"],
                    "visual_assets": [],
                    "importance_level": 2,
                },
            ]
        }
    }

    result = LayoutAgent()(state, mode="initial")
    sections = {
        element["section_id"]: element
        for element in result["initial_layout_data"]
        if element.get("type") == "section_container"
    }

    assert result["resolved_layout_template"] == "adaptive_three_column"
    assert sections["method"]["width"] > sections["intro"]["width"]
    assert sections["method"]["width"] > sections["results"]["width"]


def test_visual_legibility_heuristic_requests_middle_lane_for_wide_visual():
    state = create_state("/tmp/paper.pdf", enable_visual_legibility_review=True, enable_adaptive_column_width=True)
    state["layout_template_metadata"] = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "three_column_postergen",
        header_height=6.0,
    )
    middle_lane = state["layout_template_metadata"]["lanes"][1]
    state["visual_assets"] = {
        "figure_2": {
            "asset_id": "figure_2",
            "caption": "Hierarchical method pipeline overview",
            "asset_type": "figure",
        }
    }
    state["styled_layout"] = [
        {
            "type": "visual",
            "id": "method_figure",
            "slot_id": "method_figure",
            "visual_id": "figure_2",
            "x": middle_lane["x"] + 0.3,
            "y": middle_lane["y"] + 1.0,
            "width": 15.0,
            "height": 5.0,
        }
    ]

    review = VisualLegibilityReviewer()._heuristic_review(state)

    assert review["needs_relayout"] is True
    assert review["layout_recommendation"]["target_lane"] == "middle"


def test_adaptive_column_relayout_sets_template_and_saves_decision(tmp_path):
    state = create_state(
        str(tmp_path / "paper.pdf"),
        enable_visual_legibility_review=True,
        enable_adaptive_column_width=True,
    )
    state["output_dir"] = str(tmp_path / "output")
    state["adaptive_relayout_required"] = True
    state["visual_legibility_review"] = {
        "needs_relayout": True,
        "layout_recommendation": {
            "target_lane": "middle",
            "action": "widen_lane",
            "preferred_width_ratio": 1.3,
            "reason": "Middle visual text is too small.",
        },
        "issues": [],
    }

    result = AdaptiveColumnRelayoutAgent()(state)

    assert result["layout_template"] == "adaptive_three_column"
    assert result["adaptive_relayout_count"] == 1
    assert result["adaptive_lane_widths"]["middle"] > result["adaptive_lane_widths"]["left"]
    assert Path(state["output_dir"], "content", "adaptive_layout_decision.json").exists()


def test_layout_agent_single_column_template_stacks_semantic_lanes():
    state = create_state("/tmp/paper.pdf", layout_template="single_column_vertical")
    state["narrative_content"] = {"meta": {"poster_title": "Paper", "authors": "Authors"}}
    state["story_board"] = {
        "spatial_content_plan": {
            "sections": [
                {
                    "section_id": "intro",
                    "section_title": "Intro",
                    "column_assignment": "left",
                    "vertical_priority": "top",
                    "text_content": ["Point A", "Point B"],
                    "visual_assets": [],
                    "importance_level": 2,
                },
                {
                    "section_id": "method",
                    "section_title": "Method",
                    "column_assignment": "middle",
                    "vertical_priority": "top",
                    "text_content": ["Point A", "Point B"],
                    "visual_assets": [],
                    "importance_level": 1,
                },
                {
                    "section_id": "results",
                    "section_title": "Results",
                    "column_assignment": "right",
                    "vertical_priority": "top",
                    "text_content": ["Point A", "Point B"],
                    "visual_assets": [],
                    "importance_level": 2,
                },
            ]
        }
    }

    result = LayoutAgent()(state, mode="initial")
    sections = {
        element["section_id"]: element
        for element in result["initial_layout_data"]
        if element.get("type") == "section_container"
    }

    assert sections["intro"]["x"] == sections["method"]["x"] == sections["results"]["x"]
    assert sections["intro"]["y"] < sections["method"]["y"] < sections["results"]["y"]


def test_micro_layout_refiner_packs_overflowing_lane_without_lane_overflow():
    state = create_state("/tmp/paper.pdf", layout_template="three_column_postergen")
    state["layout_template_metadata"] = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "three_column_postergen",
        header_height=(36 - 2) * 0.18,
    )
    state["styled_layout"] = [
        {
            "type": "section_container",
            "section_id": "s1",
            "lane_id": "left",
            "x": 1.0,
            "y": 7.12,
            "width": 16.66,
            "height": 12.0,
            "importance_level": 2,
            "priority": 0.1,
        },
        {
            "type": "text",
            "id": "s1_text",
            "x": 1.3,
            "y": 8.0,
            "width": 16.06,
            "height": 11.0,
            "content": "Point A\nPoint B\nPoint C\nPoint D\nPoint E\nPoint F",
            "font_family": "Arial",
            "font_size": 44,
            "font_color": "#000000",
            "priority": 0.5,
        },
        {
            "type": "section_container",
            "section_id": "s2",
            "lane_id": "left",
            "x": 1.0,
            "y": 20.5,
            "width": 16.66,
            "height": 12.0,
            "importance_level": 2,
            "priority": 0.1,
        },
        {
            "type": "text",
            "id": "s2_text",
            "x": 1.3,
            "y": 21.3,
            "width": 16.06,
            "height": 10.8,
            "content": "Point A\nPoint B\nPoint C\nPoint D\nPoint E\nPoint F",
            "font_family": "Arial",
            "font_size": 44,
            "font_color": "#000000",
            "priority": 0.5,
        },
    ]

    result = MicroLayoutRefiner()(state)
    left_lane = state["layout_template_metadata"]["lanes"][0]
    left_sections = [
        element for element in result["styled_layout"]
        if element.get("type") == "section_container" and element.get("lane_id") == "left"
    ]

    assert left_sections
    assert max(section["y"] + section["height"] for section in left_sections) <= left_lane["y"] + left_lane["h"] + 0.05


def test_micro_layout_refiner_expands_underfilled_lane():
    state = create_state("/tmp/paper.pdf", layout_template="three_column_postergen")
    state["layout_template_metadata"] = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "three_column_postergen",
        header_height=(36 - 2) * 0.18,
    )
    left_lane = state["layout_template_metadata"]["lanes"][0]
    state["styled_layout"] = [
        {
            "type": "section_container",
            "section_id": "s1",
            "lane_id": "left",
            "x": left_lane["x"],
            "y": left_lane["y"],
            "width": left_lane["w"],
            "height": 4.0,
            "importance_level": 2,
            "priority": 0.1,
        },
        {
            "type": "section_title",
            "id": "s1_title",
            "x": left_lane["x"] + 0.2,
            "y": left_lane["y"] + 0.2,
            "width": left_lane["w"] - 0.4,
            "height": 1.0,
            "font_size": 64,
            "priority": 0.2,
        },
        {
            "type": "text",
            "id": "s1_text",
            "x": left_lane["x"] + 0.3,
            "y": left_lane["y"] + 1.4,
            "width": left_lane["w"] - 0.6,
            "height": 2.0,
            "content": "Point A\nPoint B\nPoint C",
            "font_family": "Arial",
            "font_size": 44,
            "font_color": "#000000",
            "priority": 0.5,
        },
        {
            "type": "section_container",
            "section_id": "s2",
            "lane_id": "left",
            "x": left_lane["x"],
            "y": left_lane["y"] + 5.0,
            "width": left_lane["w"],
            "height": 4.0,
            "importance_level": 2,
            "priority": 0.1,
        },
        {
            "type": "section_title",
            "id": "s2_title",
            "x": left_lane["x"] + 0.2,
            "y": left_lane["y"] + 5.2,
            "width": left_lane["w"] - 0.4,
            "height": 1.0,
            "font_size": 64,
            "priority": 0.2,
        },
        {
            "type": "text",
            "id": "s2_text",
            "x": left_lane["x"] + 0.3,
            "y": left_lane["y"] + 6.4,
            "width": left_lane["w"] - 0.6,
            "height": 2.0,
            "content": "Point D\nPoint E\nPoint F",
            "font_family": "Arial",
            "font_size": 44,
            "font_color": "#000000",
            "priority": 0.5,
        },
    ]

    original_bottom = max(
        element["y"] + element["height"]
        for element in state["styled_layout"]
        if element.get("type") == "section_container"
    )
    result = MicroLayoutRefiner()(state)
    refined_sections = [
        element for element in result["styled_layout"]
        if element.get("type") == "section_container" and element.get("lane_id") == "left"
    ]
    refined_bottom = max(section["y"] + section["height"] for section in refined_sections)

    assert refined_bottom > original_bottom
    assert refined_bottom <= left_lane["y"] + left_lane["h"] + 0.05


def test_micro_layout_refiner_validation_rejects_child_outside_container():
    state = create_state("/tmp/paper.pdf", layout_template="three_column_postergen")
    template = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "three_column_postergen",
        header_height=(36 - 2) * 0.18,
    )
    lane_map = {lane["id"]: lane for lane in template["lanes"]}
    lane = lane_map["left"]
    layout = [
        {
            "type": "section_container",
            "section_id": "hags_core",
            "lane_id": "left",
            "x": lane["x"],
            "y": lane["y"],
            "width": lane["w"],
            "height": 3.0,
        },
        {
            "type": "text",
            "id": "hags_core_text",
            "x": lane["x"] + 0.3,
            "y": lane["y"] + 1.0,
            "width": lane["w"] - 0.6,
            "height": 3.5,
        },
    ]

    validation = MicroLayoutRefiner()._validate_refined_layout(layout, lane_map, state)

    assert any("child vertical overflow" in issue for issue in validation["issues"])


def test_vlm_layout_reviewer_syncs_container_after_patch():
    state = create_state("/tmp/paper.pdf", layout_template="three_column_postergen", enable_vlm_layout_review=True)
    template = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "three_column_postergen",
        header_height=(36 - 2) * 0.18,
    )
    state["layout_template_metadata"] = template
    lane = template["lanes"][0]
    layout = [
        {
            "type": "section_container",
            "section_id": "hags_core",
            "lane_id": "left",
            "x": lane["x"],
            "y": lane["y"],
            "width": lane["w"],
            "height": 4.0,
        },
        {
            "type": "text",
            "id": "hags_core_text",
            "x": lane["x"] + 0.3,
            "y": lane["y"] + 1.0,
            "width": lane["w"] - 0.6,
            "height": 3.8,
            "font_size": 44,
        },
    ]
    patch = [{"target": "hags_core_text", "op": "increase_font_size", "value": 2}]

    patched = VLMLayoutReviewer()._apply_safe_patch(layout, patch, state)
    container = next(element for element in patched if element.get("type") == "section_container")
    text = next(element for element in patched if element.get("id") == "hags_core_text")

    assert patched is not None
    assert container["y"] + container["height"] >= text["y"] + text["height"]


def test_micro_layout_refiner_handles_two_plus_one_mixed_without_right_lane_overflow():
    state = create_state("/tmp/paper.pdf", layout_template="two_plus_one_mixed")
    state["layout_template_metadata"] = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "two_plus_one_mixed",
        header_height=(36 - 2) * 0.18,
    )
    right_lane = state["layout_template_metadata"]["lanes"][2]
    state["styled_layout"] = [
        {
            "type": "section_container",
            "section_id": "results",
            "lane_id": "right",
            "x": right_lane["x"],
            "y": right_lane["y"],
            "width": right_lane["w"],
            "height": right_lane["h"] * 0.75,
            "importance_level": 1,
            "priority": 0.1,
        },
        {
            "type": "section_title",
            "id": "results_title",
            "x": right_lane["x"] + 0.2,
            "y": right_lane["y"] + 0.2,
            "width": right_lane["w"] - 0.4,
            "height": 1.0,
            "font_size": 64,
            "priority": 0.2,
        },
        {
            "type": "visual",
            "id": "results_visual",
            "slot_id": "results_visual",
            "visual_id": "table_1",
            "x": right_lane["x"] + 0.4,
            "y": right_lane["y"] + 1.6,
            "width": right_lane["w"] - 0.8,
            "height": 7.5,
            "priority": 0.4,
        },
        {
            "type": "text",
            "id": "results_text",
            "x": right_lane["x"] + 0.3,
            "y": right_lane["y"] + 9.6,
            "width": right_lane["w"] - 0.6,
            "height": 15.0,
            "content": "\n".join([f"Result point {i}" for i in range(1, 20)]),
            "font_family": "Arial",
            "font_size": 44,
            "font_color": "#000000",
            "priority": 0.5,
        },
    ]

    result = MicroLayoutRefiner()(state)
    right_sections = [
        element for element in result["styled_layout"]
        if element.get("type") == "section_container" and element.get("lane_id") == "right"
    ]

    assert right_sections
    assert max(section["y"] + section["height"] for section in right_sections) <= right_lane["y"] + right_lane["h"] + 0.05


def test_micro_layout_refiner_handles_one_plus_two_mixed_without_left_lane_overflow():
    state = create_state("/tmp/paper.pdf", layout_template="one_plus_two_mixed")
    state["layout_template_metadata"] = LayoutTemplates(54, 36, margin=1.0, col_gap=1.0).get_template(
        "one_plus_two_mixed",
        header_height=(36 - 2) * 0.18,
    )
    left_lane = state["layout_template_metadata"]["lanes"][0]
    state["styled_layout"] = [
        {
            "type": "section_container",
            "section_id": "intro",
            "lane_id": "left",
            "x": left_lane["x"],
            "y": left_lane["y"],
            "width": left_lane["w"],
            "height": left_lane["h"] * 0.82,
            "importance_level": 2,
            "priority": 0.1,
        },
        {
            "type": "section_title",
            "id": "intro_title",
            "x": left_lane["x"] + 0.2,
            "y": left_lane["y"] + 0.2,
            "width": left_lane["w"] - 0.4,
            "height": 1.0,
            "font_size": 64,
            "priority": 0.2,
        },
        {
            "type": "text",
            "id": "intro_text",
            "x": left_lane["x"] + 0.3,
            "y": left_lane["y"] + 1.4,
            "width": left_lane["w"] - 0.6,
            "height": 18.0,
            "content": "\n".join([f"Background point {i}" for i in range(1, 26)]),
            "font_family": "Arial",
            "font_size": 44,
            "font_color": "#000000",
            "priority": 0.5,
        },
        {
            "type": "visual",
            "id": "intro_visual",
            "slot_id": "intro_visual",
            "visual_id": "figure_1",
            "x": left_lane["x"] + 0.5,
            "y": left_lane["y"] + 20.0,
            "width": left_lane["w"] - 1.0,
            "height": 6.5,
            "priority": 0.4,
        },
    ]

    result = MicroLayoutRefiner()(state)
    left_sections = [
        element for element in result["styled_layout"]
        if element.get("type") == "section_container" and element.get("lane_id") == "left"
    ]

    assert left_sections
    assert max(section["y"] + section["height"] for section in left_sections) <= left_lane["y"] + left_lane["h"] + 0.05


def test_template_selector_prefers_balanced_three_column_layout():
    selector = TemplateSelector(load_config())
    state = create_state("/tmp/paper.pdf", layout_template="auto")
    state["visual_assets"] = {
        "figure_1": {"aspect": 2.0, "asset_type": "figure"},
        "figure_2": {"aspect": 2.1, "asset_type": "figure"},
        "table_1": {"aspect": 3.0, "asset_type": "table"},
        "table_2": {"aspect": 3.2, "asset_type": "table"},
    }
    structured_sections = {
        "paper_sections": [
            {"section_name": "Introduction", "section_type": "foundation", "key_points": ["A", "B"], "contains_figures": ["figure_1"], "contains_tables": []},
            {"section_name": "Method", "section_type": "method", "key_points": ["A", "B"], "contains_figures": ["figure_2"], "contains_tables": []},
            {"section_name": "Results", "section_type": "evaluation", "key_points": ["A", "B"], "contains_figures": [], "contains_tables": ["table_1", "table_2"]},
        ]
    }
    classified_visuals = {
        "key_visual": "figure_2",
        "problem_illustration": ["figure_1"],
        "method_workflow": ["figure_2"],
        "main_results": ["table_1"],
        "comparative_results": ["table_2"],
        "supporting": [],
    }

    result = selector.select(state, structured_sections, classified_visuals, state["visual_assets"])

    assert result["selected_template"] in {"three_column_postergen", "two_plus_one_mixed", "one_plus_two_mixed"}
    assert result["selected_template"] == "three_column_postergen"


def test_template_selector_exposes_right_heavy_lane_preference_in_auto_mode():
    selector = TemplateSelector(load_config())
    state = create_state("/tmp/paper.pdf", layout_template="auto")
    state["visual_assets"] = {
        "figure_1": {"aspect": 2.4, "asset_type": "figure"},
        "figure_2": {"aspect": 2.4, "asset_type": "figure"},
        "table_1": {"aspect": 1.6, "asset_type": "table"},
        "table_2": {"aspect": 1.7, "asset_type": "table"},
    }
    structured_sections = {
        "paper_sections": [
            {"section_name": "Introduction", "section_type": "foundation", "key_points": ["A"], "contains_figures": ["figure_1"], "contains_tables": []},
            {"section_name": "Method", "section_type": "method", "key_points": ["A"], "contains_figures": ["figure_2"], "contains_tables": []},
            {
                "section_name": "Results",
                "section_type": "evaluation",
                "key_points": ["A", "B", "C", "D", "E", "F", "G"],
                "contains_figures": [],
                "contains_tables": ["table_1", "table_2"],
            },
        ]
    }
    classified_visuals = {
        "key_visual": "figure_2",
        "problem_illustration": ["figure_1"],
        "method_workflow": ["figure_2"],
        "main_results": ["table_1"],
        "comparative_results": ["table_2"],
        "supporting": [],
    }

    result = selector.select(state, structured_sections, classified_visuals, state["visual_assets"])

    assert result["preferred_template"] == "two_plus_one_mixed"
    assert result["selected_template"] in {"three_column_postergen", "two_plus_one_mixed", "one_plus_two_mixed"}


def test_template_selector_respects_manual_template_request():
    selector = TemplateSelector(load_config())
    state = create_state("/tmp/paper.pdf", layout_template="one_plus_two_mixed")

    result = selector.select(
        state,
        structured_sections={"paper_sections": []},
        classified_visuals={},
        visual_assets={},
    )

    assert result["selection_mode"] == "manual"
    assert result["selected_template"] == "one_plus_two_mixed"
