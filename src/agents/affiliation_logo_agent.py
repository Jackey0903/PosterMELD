"""Affiliation logo discovery and caching."""

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont

from src.config.poster_config import load_config
from src.state.poster_state import PosterState
from utils.src.logging_utils import log_agent_error, log_agent_info, log_agent_success, log_agent_warning


class AffiliationLogoAgent:
    """Resolve paper affiliations into local logo assets.

    The agent deliberately runs before layout construction. Layout elements should
    receive already-local image paths, while the renderer only paints those paths.
    """

    def __init__(self):
        self.name = "affiliation_logo_agent"
        self.config = load_config().get("affiliation_logos", {})
        self.timeout = self.config.get("request_timeout_seconds", 20)
        self.max_logos = self.config.get("max_logos", 4)
        self.clearbit_base_url = self.config.get("clearbit_base_url", "https://logo.clearbit.com").rstrip("/")
        self.known_domains = self.config.get("known_domains", {})
        self.official_logo_urls = self.config.get("official_logo_urls", {})
        self.known_commons_files = self.config.get("known_commons_files", {})
        self.local_dirs = self.config.get("local_dirs", ["affiliation_logos", "logos"])
        self.min_logo_long_edge = int(self.config.get("min_logo_long_edge", 320))
        self.normalized_max_size = tuple(self.config.get("normalized_max_size", [1800, 720]))
        # populated by _try_openalex_institutions; maps institution name → Wikidata QID URL
        self._openalex_wikidata_cache: dict[str, str] = {}

    def __call__(self, state: PosterState) -> PosterState:
        if not state.get("enable_affiliation_logos", False):
            state["affiliation_logos"] = []
            self._save_outputs(state, [], state.get("affiliations") or [])
            return state

        try:
            affiliations = self._get_affiliations(state)
            output_dir = Path(state["output_dir"])
            logo_dir = output_dir / "assets" / "affiliation_logos"
            logo_dir.mkdir(parents=True, exist_ok=True)

            logos: List[Dict[str, Any]] = []
            seen_logo_keys = set()
            for affiliation in affiliations:
                if len(logos) >= self.max_logos:
                    break
                logo_key = self._canonical_logo_key(affiliation)
                if logo_key in seen_logo_keys:
                    continue
                entry = self._resolve_logo(affiliation, logo_dir)
                if entry:
                    logos.append(entry)
                    seen_logo_keys.add(logo_key)

            state["affiliation_logos"] = logos
            if logos and not (state.get("aff_logo_path") and Path(state["aff_logo_path"]).exists()):
                state["aff_logo_path"] = logos[0]["logo_path"]
            state["current_agent"] = self.name
            self._save_outputs(state, logos, affiliations)
            log_agent_success(self.name, f"resolved {len(logos)} affiliation logos")
        except Exception as e:
            log_agent_error(self.name, f"failed: {e}")
            state["errors"].append(f"{self.name}: {e}")

        return state

    def _get_affiliations(self, state: PosterState) -> List[str]:
        # If DOI is available, try OpenAlex to get authoritative institution names
        doi = state.get("doi")
        if doi:
            openalex_insts = self._try_openalex_institutions(doi)
            if openalex_insts:
                log_agent_info(self.name, f"OpenAlex returned {len(openalex_insts)} institutions for DOI {doi}")
                # Merge: OpenAlex names take precedence, then any parser-extracted extras
                seen = {n.lower() for n in openalex_insts}
                for name in (state.get("affiliations") or []):
                    if name.lower() not in seen:
                        openalex_insts.append(name)
                        seen.add(name.lower())
                return openalex_insts[:6]

        affiliations = state.get("affiliations") or []
        if not affiliations:
            narrative = state.get("narrative_content") or {}
            affiliations = narrative.get("meta", {}).get("affiliations", [])

        deduped: List[str] = []
        seen = set()
        for name in affiliations:
            normalized = self._normalize_name(str(name))
            key = normalized.lower()
            if normalized and key not in seen:
                deduped.append(normalized)
                seen.add(key)
        return deduped

    def _try_openalex_institutions(self, doi: str) -> List[str]:
        """Query OpenAlex for authoritative institution names for a DOI."""
        try:
            url = f"https://api.openalex.org/works/doi:{doi}"
            resp = requests.get(url, timeout=self.timeout, headers={"User-Agent": "Paper2Poster/1.0"})
            if resp.status_code != 200:
                return []
            data = resp.json()
            seen: set[str] = set()
            names: List[str] = []
            for authorship in data.get("authorships", []):
                for inst in authorship.get("institutions", []):
                    raw_name = inst.get("display_name", "").strip()
                    wikidata_id = inst.get("ids", {}).get("wikidata", "")
                    if not raw_name:
                        continue
                    key = raw_name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    # Store wikidata ID alongside name so _resolve_logo can use it
                    self._openalex_wikidata_cache[raw_name] = wikidata_id
                    names.append(raw_name)
            return names
        except Exception:
            return []

    def _resolve_logo(self, institution: str, logo_dir: Path) -> Optional[Dict[str, Any]]:
        institution = self._canonical_institution_name(institution)
        domain = self._resolve_domain(institution)
        slug = self._slugify(institution)
        output_path = logo_dir / f"{slug}.png"

        local_logo = self._find_local_logo(institution, logo_dir.parent.parent)
        if local_logo:
            cached = self._copy_logo_to_cache(local_logo, output_path)
            if cached:
                return self._make_logo_entry(institution, domain, cached, "local_asset", "resolved")

        official_logo = self._download_official_logo(institution, output_path)
        if official_logo:
            return self._make_logo_entry(institution, domain, official_logo, "official_site", "resolved")

        known_commons_logo = self._download_known_commons_logo(institution, output_path)
        if known_commons_logo:
            return self._make_logo_entry(institution, domain, known_commons_logo, "wikimedia_commons_known", "resolved")

        wikidata_logo = self._download_wikidata_logo(institution, output_path)
        if wikidata_logo:
            return self._make_logo_entry(institution, domain, wikidata_logo, "wikimedia_commons", "resolved")

        if domain:
            downloaded = self._download_clearbit_logo(domain, output_path)
            if downloaded:
                return self._make_logo_entry(institution, domain, downloaded, "clearbit", "resolved")
            log_agent_warning(self.name, f"logo download failed for {institution} ({domain})")

        if self.config.get("include_placeholders", True):
            placeholder = self._create_placeholder_logo(institution, output_path)
            return self._make_logo_entry(institution, domain, placeholder, "placeholder", "placeholder")

        return None

    def _resolve_domain(self, institution: str) -> Optional[str]:
        institution = self._canonical_institution_name(institution)
        if institution in self.known_domains:
            return self.known_domains[institution]

        lowered = institution.lower()
        for known_name, domain in self.known_domains.items():
            if lowered == known_name.lower() or lowered in known_name.lower() or known_name.lower() in lowered:
                return domain

        return self._guess_domain(institution)

    def _canonical_logo_key(self, institution: str) -> str:
        local_logo = self._find_local_logo(institution, None)
        if local_logo:
            return f"local:{local_logo.resolve()}"
        commons_file = self._resolve_known_commons_file(institution)
        if commons_file:
            return f"commons:{commons_file.lower()}"
        domain = self._resolve_domain(institution)
        if domain:
            return f"domain:{domain.lower()}"
        return f"name:{institution.lower()}"

    def _guess_domain(self, institution: str) -> Optional[str]:
        name = institution.lower()
        aliases = {
            "washington university in st. louis": "wustl.edu",
            "washington university": "wustl.edu",
            "george mason university": "gmu.edu",
            "tsinghua university": "tsinghua.edu.cn",
            "beijing university of posts and telecommunications": "bupt.edu.cn",
            "the chinese university of hong kong": "cuhk.edu.hk",
            "chinese university of hong kong": "cuhk.edu.hk",
            "university of illinois chicago": "uic.edu",
            "university of illinois at chicago": "uic.edu",
            "hong kong university of science and technology guangzhou": "hkust-gz.edu.cn",
            "hong kong university of science and technology": "hkust.edu.hk",
        }
        for key, domain in aliases.items():
            if key in name:
                return domain

        compact = re.sub(r"[^a-z0-9 ]", "", name)
        compact = re.sub(r"\b(the|of|at|in|and|school|college|department|division)\b", "", compact)
        words = [word for word in compact.split() if word]
        if len(words) >= 2 and any(token in name for token in ("university", "institute", "college")):
            return "".join(word[0] for word in words[:4]) + ".edu"
        return None

    def _find_local_logo(self, institution: str, output_dir: Optional[Path]) -> Optional[Path]:
        candidates: List[Path] = []
        if output_dir:
            pdf_root = Path(output_dir).parent.parent / "data" / Path(output_dir).name
            candidates.extend(self._candidate_local_dirs(pdf_root))

        slug = self._slugify(institution)
        name_tokens = set(slug.split("-"))
        for directory in candidates:
            if not directory.exists():
                continue
            files = [
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}
            ]
            for path in files:
                stem = self._slugify(path.stem)
                if stem == slug or slug in stem or stem in slug:
                    return path
            for path in files:
                stem_tokens = set(self._slugify(path.stem).split("-"))
                if len(name_tokens & stem_tokens) >= 2:
                    return path
        return None

    def _candidate_local_dirs(self, paper_dir: Path) -> List[Path]:
        dirs = [paper_dir]
        dirs.extend(paper_dir / dirname for dirname in self.local_dirs)
        return dirs

    def _copy_logo_to_cache(self, source_path: Path, output_path: Path) -> str:
        if output_path.exists() and output_path.stat().st_size > 0:
            return str(output_path)
        if source_path.suffix.lower() == ".svg":
            if not self._convert_svg_to_png(source_path, output_path):
                return ""
        else:
            with Image.open(source_path) as img:
                img.convert("RGBA").save(output_path, "PNG")
        if not self._normalize_image_file(output_path):
            return ""
        return str(output_path)

    def _download_clearbit_logo(self, domain: str, output_path: Path) -> Optional[str]:
        if output_path.exists() and output_path.stat().st_size > 0:
            return str(output_path)

        url = f"{self.clearbit_base_url}/{domain}"
        try:
            response = requests.get(url, timeout=self.timeout, headers={"User-Agent": "Paper2Poster/1.0"})
            response.raise_for_status()
            if "image" not in response.headers.get("content-type", ""):
                return None
            content_type = response.headers.get("content-type", "")
            if "svg" in content_type:
                svg_path = output_path.with_suffix(".svg")
                svg_path.write_bytes(response.content)
                if not self._convert_svg_to_png(svg_path, output_path):
                    svg_path.unlink(missing_ok=True)
                    return None
                svg_path.unlink(missing_ok=True)
            else:
                output_path.write_bytes(response.content)
            if not self._normalize_image_file(output_path):
                output_path.unlink(missing_ok=True)
                return None
            return str(output_path)
        except Exception:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            return None

    def _download_official_logo(self, institution: str, output_path: Path) -> Optional[str]:
        urls = self._resolve_official_logo_urls(institution)
        for url in urls:
            downloaded = self._download_url_logo(url, output_path)
            if downloaded:
                return downloaded
        return None

    def _resolve_official_logo_urls(self, institution: str) -> List[str]:
        if institution in self.official_logo_urls:
            return list(self.official_logo_urls[institution])
        lowered = institution.lower()
        for known_name, urls in self.official_logo_urls.items():
            known_lowered = known_name.lower()
            if lowered == known_lowered or lowered in known_lowered or known_lowered in lowered:
                return list(urls)
        return []

    def _download_url_logo(self, url: str, output_path: Path) -> Optional[str]:
        if output_path.exists() and output_path.stat().st_size > 0:
            return str(output_path)
        try:
            try:
                response = requests.get(url, timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0 Paper2Poster/1.0"})
            except requests.exceptions.SSLError:
                response = requests.get(url, timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0 Paper2Poster/1.0"}, verify=False)
            response.raise_for_status()
            if "image" not in response.headers.get("content-type", ""):
                return None
            content_type = response.headers.get("content-type", "")
            if "svg" in content_type or url.lower().split("?", 1)[0].endswith(".svg"):
                svg_path = output_path.with_suffix(".svg")
                svg_path.write_bytes(response.content)
                if not self._convert_svg_to_png(svg_path, output_path):
                    svg_path.unlink(missing_ok=True)
                    return None
                svg_path.unlink(missing_ok=True)
            else:
                output_path.write_bytes(response.content)
            if not self._normalize_image_file(output_path):
                output_path.unlink(missing_ok=True)
                return None
            return str(output_path)
        except Exception:
            output_path.unlink(missing_ok=True)
            return None

    def _download_known_commons_logo(self, institution: str, output_path: Path) -> Optional[str]:
        filename = self._resolve_known_commons_file(institution)
        if not filename:
            return None
        return self._download_commons_file(filename, output_path)

    def _resolve_known_commons_file(self, institution: str) -> Optional[str]:
        if institution in self.known_commons_files:
            return self.known_commons_files[institution]
        lowered = institution.lower()
        for known_name, filename in self.known_commons_files.items():
            known_lowered = known_name.lower()
            if lowered == known_lowered or lowered in known_lowered or known_lowered in lowered:
                return filename
        return None

    def _download_commons_file(self, filename: str, output_path: Path) -> Optional[str]:
        try:
            file_url = self._get_commons_thumbnail_url(filename) or f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename)}?width=1800"
            response = requests.get(file_url, timeout=self.timeout, headers={"User-Agent": "Paper2Poster/1.0"})
            response.raise_for_status()
            if "image" not in response.headers.get("content-type", ""):
                return None
            content_type = response.headers.get("content-type", "")
            if "svg" in content_type:
                svg_path = output_path.with_suffix(".svg")
                svg_path.write_bytes(response.content)
                if not self._convert_svg_to_png(svg_path, output_path):
                    svg_path.unlink(missing_ok=True)
                    return None
                svg_path.unlink(missing_ok=True)
            else:
                output_path.write_bytes(response.content)
            if not self._normalize_image_file(output_path):
                output_path.unlink(missing_ok=True)
                return None
            return str(output_path)
        except Exception:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            return None

    def _get_commons_thumbnail_url(self, filename: str) -> Optional[str]:
        """Resolve a high-resolution raster thumbnail URL for PNG/SVG Commons files."""
        params = {
            "action": "query",
            "format": "json",
            "titles": f"File:{filename}",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": 1800,
        }
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": "Paper2Poster/1.0"},
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            return info.get("thumburl") or info.get("url")
        return None

    def _download_wikidata_logo(self, institution: str, output_path: Path) -> Optional[str]:
        if output_path.exists() and output_path.stat().st_size > 0:
            return str(output_path)

        try:
            # Prefer the QID from OpenAlex (exact entity, no fuzzy search)
            cached_qid_url = self._openalex_wikidata_cache.get(institution, "")
            if cached_qid_url:
                # QID URL looks like "https://www.wikidata.org/entity/Q49117"
                entity_id = cached_qid_url.rstrip("/").rsplit("/", 1)[-1]
            else:
                entity_id = self._search_wikidata_entity(institution)
            if not entity_id:
                return None
            filename = self._get_wikidata_image_filename(entity_id)
            if not filename:
                return None
            return self._download_commons_file(filename, output_path)
        except Exception:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            return None

    def _search_wikidata_entity(self, institution: str) -> Optional[str]:
        params = {
            "action": "wbsearchentities",
            "search": institution,
            "language": "en",
            "format": "json",
            "limit": 3,
        }
        response = requests.get(
            "https://www.wikidata.org/w/api.php",
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": "Paper2Poster/1.0"},
        )
        response.raise_for_status()
        for item in response.json().get("search", []):
            label = item.get("label", "").lower()
            description = item.get("description", "").lower()
            if any(token in f"{label} {description}" for token in ("university", "school", "college", "institution")):
                return item.get("id")
        search = response.json().get("search", [])
        return search[0].get("id") if search else None

    def _get_wikidata_image_filename(self, entity_id: str) -> Optional[str]:
        params = {
            "action": "wbgetentities",
            "ids": entity_id,
            "props": "claims",
            "format": "json",
        }
        response = requests.get(
            "https://www.wikidata.org/w/api.php",
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": "Paper2Poster/1.0"},
        )
        response.raise_for_status()
        claims = response.json().get("entities", {}).get(entity_id, {}).get("claims", {})
        for property_id in ("P154", "P94"):
            for claim in claims.get(property_id, []):
                value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(value, str):
                    return value
        return None

    def _normalize_image_file(self, path: Path) -> bool:
        try:
            with Image.open(path) as img:
                img = img.convert("RGBA")
                img = self._trim_logo_whitespace(img)
                if not self._is_usable_logo_image(img) and self._looks_like_white_transparent_logo(img):
                    img = self._recolor_visible_pixels(img, self.config.get("monochrome_logo_color", "#1E3A8A"))
                    img = self._trim_logo_whitespace(img)
                if not self._is_usable_logo_image(img):
                    return False
                img.thumbnail(self.normalized_max_size, Image.LANCZOS)
                canvas = Image.new("RGBA", img.size, (255, 255, 255, 0))
                canvas.alpha_composite(img)
                canvas.save(path, "PNG")
            return True
        except Exception:
            return False

    def _is_usable_logo_image(self, img: Image.Image) -> bool:
        width, height = img.size
        if max(width, height) < self.min_logo_long_edge:
            return False
        if min(width, height) < 32:
            return False
        pixels = img.load()
        visible = 0
        non_white = 0
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]
                if a > 12:
                    visible += 1
                    if not (r > 246 and g > 246 and b > 246):
                        non_white += 1
        return visible > 0 and non_white / max(visible, 1) > 0.01

    def _looks_like_white_transparent_logo(self, img: Image.Image) -> bool:
        pixels = img.load()
        width, height = img.size
        visible = 0
        near_white = 0
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]
                if a > 12:
                    visible += 1
                    if r > 235 and g > 235 and b > 235:
                        near_white += 1
        return visible > 0 and near_white / visible > 0.96

    def _recolor_visible_pixels(self, img: Image.Image, hex_color: str) -> Image.Image:
        hex_color = str(hex_color).lstrip("#")
        try:
            target = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            target = (30, 58, 138)
        recolored = Image.new("RGBA", img.size, (255, 255, 255, 0))
        src = img.load()
        dst = recolored.load()
        for y in range(img.size[1]):
            for x in range(img.size[0]):
                r, g, b, a = src[x, y]
                if a > 12:
                    dst[x, y] = (*target, a)
        return recolored

    def _convert_svg_to_png(self, source_path: Path, output_path: Path) -> bool:
        try:
            import cairosvg  # type: ignore

            cairosvg.svg2png(url=str(source_path), write_to=str(output_path), output_width=1800)
            return True
        except Exception:
            convert = shutil.which("rsvg-convert")
            if not convert:
                return False
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                import subprocess

                subprocess.run([convert, "-w", "1800", "-o", str(tmp_path), str(source_path)], check=True)
                tmp_path.replace(output_path)
                return True
            except Exception:
                tmp_path.unlink(missing_ok=True)
                return False

    def _trim_logo_whitespace(self, img: Image.Image) -> Image.Image:
        pixels = img.load()
        width, height = img.size
        xs: List[int] = []
        ys: List[int] = []
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]
                is_visible = a > 12
                is_non_white = not (r > 244 and g > 244 and b > 244)
                if is_visible and is_non_white:
                    xs.append(x)
                    ys.append(y)
        if not xs or not ys:
            return img
        pad_x = max(4, int((max(xs) - min(xs) + 1) * 0.05))
        pad_y = max(4, int((max(ys) - min(ys) + 1) * 0.08))
        box = (
            max(min(xs) - pad_x, 0),
            max(min(ys) - pad_y, 0),
            min(max(xs) + pad_x + 1, width),
            min(max(ys) + pad_y + 1, height),
        )
        return img.crop(box)

    def _create_placeholder_logo(self, institution: str, output_path: Path) -> str:
        initials = self._initials(institution)
        width, height = 640, 260
        bg = (238, 242, 248, 255)
        accent = (38, 74, 120, 255)
        image = Image.new("RGBA", (width, height), bg)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((14, 14, width - 14, height - 14), radius=34, outline=accent, width=6)

        try:
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 96)
            small_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 30)
        except Exception:
            font = ImageFont.load_default()
            small_font = ImageFont.load_default()

        text_box = draw.textbbox((0, 0), initials, font=font)
        draw.text(
            ((width - (text_box[2] - text_box[0])) / 2, 50),
            initials,
            fill=accent,
            font=font,
        )
        label = self._short_label(institution)
        label_box = draw.textbbox((0, 0), label, font=small_font)
        draw.text(
            ((width - (label_box[2] - label_box[0])) / 2, 174),
            label,
            fill=(54, 63, 75, 255),
            font=small_font,
        )
        image.save(output_path, "PNG")
        return str(output_path)

    def _make_logo_entry(
        self,
        institution: str,
        domain: Optional[str],
        logo_path: str,
        source: str,
        status: str,
    ) -> Dict[str, Any]:
        aspect = 1.0
        try:
            with Image.open(logo_path) as img:
                aspect = img.size[0] / max(img.size[1], 1)
        except Exception:
            pass
        return {
            "institution": institution,
            "domain": domain,
            "logo_path": logo_path,
            "source": source,
            "status": status,
            "aspect": aspect,
        }

    def _save_outputs(self, state: PosterState, logos: List[Dict[str, Any]], affiliations: List[str]) -> None:
        content_dir = Path(state["output_dir"]) / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        (content_dir / "affiliations.json").write_text(
            json.dumps(affiliations, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (content_dir / "affiliation_logos.json").write_text(
            json.dumps(logos, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _normalize_name(self, name: str) -> str:
        return self._canonical_institution_name(re.sub(r"\s+", " ", name).strip(" ,.;"))

    def _canonical_institution_name(self, name: str) -> str:
        normalized = re.sub(r"\s+", " ", str(name)).strip(" ,.;")
        normalized = normalized.replace("Hongkong", "Hong Kong")
        aliases = {
            "The Chinese University of Hong Kong": "The Chinese University of Hong Kong",
            "The Chinese University of Hong Kong University": "The Chinese University of Hong Kong",
            "Hong Kong University of Science and Technology (Guangzhou)": "Hong Kong University of Science and Technology (Guangzhou)",
            "Hong Kong University of Science and Technology Guangzhou": "Hong Kong University of Science and Technology (Guangzhou)",
            "University of Illinois at Chicago": "University of Illinois Chicago",
        }
        lowered = normalized.lower()
        for alias, canonical in aliases.items():
            if lowered == alias.lower():
                return canonical
        return normalized

    def _slugify(self, name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug or "affiliation-logo"

    def _initials(self, name: str) -> str:
        stop = {"the", "of", "at", "in", "and", "for", "school", "college", "department", "division"}
        words = [word for word in re.findall(r"[A-Za-z]+", name) if word.lower() not in stop]
        return "".join(word[0].upper() for word in words[:4]) or "AFF"

    def _short_label(self, name: str) -> str:
        label = re.sub(r"\s+", " ", name).strip()
        return label if len(label) <= 34 else label[:31].rstrip() + "..."


def affiliation_logo_agent_node(state: PosterState) -> Dict[str, Any]:
    result = AffiliationLogoAgent()(state)
    return {
        **state,
        "affiliation_logos": result.get("affiliation_logos", []),
        "current_agent": result.get("current_agent"),
        "errors": result.get("errors", []),
    }
