"""Conference logo resolution: name string → local PNG path."""

import re
from pathlib import Path
from typing import Optional

# Canonical slug → list of name aliases (lowercase, year-stripped)
_ALIASES: dict[str, list[str]] = {
    "cvpr":    ["cvpr", "computer vision and pattern recognition"],
    "iccv":    ["iccv", "international conference on computer vision"],
    "eccv":    ["eccv", "european conference on computer vision"],
    "neurips": ["neurips", "nips", "neural information processing systems"],
    "icml":    ["icml", "international conference on machine learning"],
    "iclr":    ["iclr", "international conference on learning representations"],
    "aaai":    ["aaai", "association for the advancement of artificial intelligence"],
    "acl":     ["acl", "annual meeting of the association for computational linguistics"],
    "emnlp":   ["emnlp", "empirical methods in natural language processing"],
    "naacl":   ["naacl", "north american chapter of the association"],
    "ijcai":   ["ijcai", "international joint conference on artificial intelligence"],
    "kdd":     ["kdd", "knowledge discovery and data mining"],
    "www":     ["www", "world wide web", "the web conference"],
    "sigir":   ["sigir", "research and development in information retrieval"],
    "mm":      ["acmmm", "acm mm", "acm multimedia"],
    "siggraph":["siggraph"],
    "wacv":    ["wacv", "winter conference on applications of computer vision"],
    "miccai":  ["miccai", "medical image computing and computer assisted intervention"],
}

_LOGO_DIR = Path(__file__).parent.parent.parent / "assets" / "conference_logos"


def _strip_year(name: str) -> str:
    return re.sub(r"\b(19|20)\d{2}\b", "", name).strip()


def _slug(name: str) -> Optional[str]:
    """Map a free-form conference name to a canonical slug, or None."""
    normalized = _strip_year(name).lower()
    # direct slug match first
    for slug in _ALIASES:
        if slug == normalized:
            return slug
    # alias substring match
    for slug, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in normalized or normalized in alias:
                return slug
    return None


def resolve_conference_logo(conference_name: str) -> Optional[str]:
    """Return absolute path to conference logo PNG, or None if not found."""
    if not conference_name:
        return None
    slug = _slug(conference_name)
    if slug is None:
        return None
    candidate = _LOGO_DIR / f"{slug}.png"
    return str(candidate) if candidate.exists() else None


def list_supported_conferences() -> list[str]:
    return sorted(_ALIASES.keys())
