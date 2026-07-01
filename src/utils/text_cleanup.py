"""Text cleanup helpers shared by layout and rendering agents."""

import re


COMMON_OCR_FIXES = {
    "Effcient": "Efficient",
    "effcient": "efficient",
    "Effciency": "Efficiency",
    "effciency": "efficiency",
}

TITLE_SMALL_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "in",
    "nor",
    "of",
    "on",
    "or",
    "per",
    "the",
    "to",
    "via",
    "vs",
    "with",
}

DANGLING_TERMINAL_WORDS = (
    "and|or|but|with|in|of|to|for|by|as|at|from|than|while|where|when|"
    "that|which|through|into|over|under|via|on|only|also|a|an|the|this|"
    "these|those|their|its|using|including|exploiting|selecting|relying|letting|local|stale|"
    "may|can|could|would|should|will|must|is|are|was|were|be|been|being|"
    "typically|generally|often|roughly|approximately|consistently|significantly"
    "|outperforming|improving|exceeding|achieving"
)


def repair_mojibake(text: str) -> str:
    """Repair common UTF-8-as-Latin-1 mojibake without touching clean text."""
    if not isinstance(text, str) or not text:
        return text

    text = (
        text.replace("â¢", "•")
        .replace("â¦", "◦")
        .replace("â", "-")
        .replace("â", "-")
    )

    if any(marker in text for marker in ("â", "Â", "Ã", "Î", "î", "ï¿½")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
            if repaired.count("\ufffd") <= text.count("\ufffd"):
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    return text


def normalize_text_for_poster(text: str) -> str:
    """Normalize generated poster text before it reaches PowerPoint."""
    if not isinstance(text, str) or not text:
        return text

    text = repair_mojibake(text)
    text = text.replace("\u00a0", " ")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("Î»", "lambda ")
    text = text.replace("î»", "lambda ")

    for wrong, right in COMMON_OCR_FIXES.items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text)

    normalized_lines = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*[•●]\s*", "• ", line.strip())
        line = re.sub(r"^\s*[◦▪▫]\s*", "◦ ", line)
        line = _strip_poster_artifact_noise(line)
        if not line:
            continue
        line = _repair_leading_bold_label(line)
        line = repair_truncated_sentence_end(line)
        normalized_lines.append(line)

    return "\n".join(normalized_lines)


def normalize_title_for_poster(title: str) -> str:
    """Repair OCR in poster titles while preserving conventional title casing."""
    if not isinstance(title, str) or not title:
        return title

    text = repair_mojibake(title)
    text = text.replace("\u00a0", " ").replace("–", "-").replace("—", "-")
    for wrong, right in COMMON_OCR_FIXES.items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text

    words = text.split()
    normalized = []
    for index, word in enumerate(words):
        stripped = word.strip()
        match = re.match(r"^([(\"'“‘]*)(.*?)([)\"'”’,:;.!?]*)$", stripped)
        if not match:
            normalized.append(stripped)
            continue
        prefix, core, suffix = match.groups()
        if 0 < index < len(words) - 1 and core.lower() in TITLE_SMALL_WORDS:
            core = core.lower()
        normalized.append(f"{prefix}{core}{suffix}")
    return " ".join(normalized)


def _strip_poster_artifact_noise(line: str) -> str:
    """Remove OCR, markdown, and metadata artifacts that should never appear on posters."""
    if not isinstance(line, str) or not line:
        return line

    original = line
    line = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", line)
    line = re.sub(r"\[[^\]]{0,80}\]\([^)]*\.(?:png|jpe?g|pdf|svg)[^)]*\)", " ", line, flags=re.IGNORECASE)
    line = re.sub(r"\b[\w./\\-]*_page_\d+_[A-Za-z]+_\d+\.(?:png|jpe?g)\b", " ", line, flags=re.IGNORECASE)
    line = re.sub(r"\b(?:[\w.-]+/)+[\w.-]+\.(?:png|jpe?g|pdf|svg)\b", " ", line, flags=re.IGNORECASE)
    line = re.sub(r"\b[\w.-]+\.(?:png|jpe?g|svg)\b", " ", line, flags=re.IGNORECASE)
    line = re.sub(r"\s*\[[0-9,\s-]+\]", "", line)

    if re.fullmatch(
        r"\s*(?:the\s+)?(?:results|values|numbers|details|comparison|performance|ablation|evaluation)\s+"
        r"(?:are|is|were|was)\s+(?:shown|provided|presented|reported|summarized|listed|given)\s+in\s+"
        r"(?:table|tables|figure|figures|fig\.?|figs\.?)\s*\d+(?:\s*(?:and|,|-|to)\s*\d+)*\.?\s*",
        line,
        flags=re.IGNORECASE,
    ):
        return ""
    if re.match(r"^\s*(?:fig(?:ure)?|table)\s*\d+[\.:]", line, flags=re.IGNORECASE):
        return ""
    line = re.sub(r"\b(?:fig(?:ure)?|table)\s*\d+[\.:]\s*[^.|;]*\.?", "", line, flags=re.IGNORECASE)
    if (
        re.search(r"\b(?:algorithm|appendix|supplement|supplementary)\b", line, flags=re.IGNORECASE)
        and re.search(r"\b(?:detailed|complete|provided|presentation|details?|see|refer)\b", line, flags=re.IGNORECASE)
    ):
        return ""

    line = re.sub(
        r"\b(?:as\s+)?(?:shown|provided|presented|reported|summarized|listed|given)\s+in\s+"
        r"(?:table|tables|figure|figures|fig\.?|figs\.?)\s*\d+(?:\s*(?:and|,|-|to)\s*\d+)*",
        "",
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(
        r"\b(?:see|cf\.?|from)\s+(?:table|tables|figure|figures|fig\.?|figs\.?)\s*\d+"
        r"(?:\s*(?:and|,|-|to)\s*\d+)*",
        "",
        line,
        flags=re.IGNORECASE,
    )
    line = re.sub(
        r"\b(?:table|tables|figure|figures|fig\.?|figs\.?)\s*\d+(?:\s*(?:and|,|-|to)\s*\d+)*[\.:]?",
        "",
        line,
        flags=re.IGNORECASE,
    )

    if line.count("|") >= 2:
        prefix = line.split("|", 1)[0].strip()
        line = prefix if len(prefix.split()) >= 4 else ""

    line = re.sub(r"\s+\bimportance\s+(?:high|medium|low)\b.*$", "", line, flags=re.IGNORECASE)
    line = re.sub(
        r"\b(?:contains_figures|contains_tables|section_name|section_type|visual_assets|source_sections)\b\s*[:=]?\s*",
        " ",
        line,
        flags=re.IGNORECASE,
    )
    if _looks_like_metadata_only(line):
        return ""
    if _looks_like_bibliography(line):
        return ""

    line = re.sub(r"\b(?:results|values|details|comparison|performance)\s+are\s+(?:in|shown|presented|provided|reported)\s*\.?", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\b(?:the\s+)?(?:table|figure)\s+(?:shows|reports|presents|summarizes)\b.*", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\s{2,}", " ", line).strip()
    line = re.sub(r"\s+([,.;:])", r"\1", line)
    line = re.sub(r"\(\s*\)", "", line)
    line = re.sub(r"\s+[,;:]\s*$", ".", line).strip()

    if re.fullmatch(
        r"(?:the\s+)?(?:results|values|details|comparison|performance)\s+(?:are|is|were|was)\.?",
        line,
        flags=re.IGNORECASE,
    ):
        return ""
    if not line or re.fullmatch(r"[\W_]+", line):
        return ""
    if original.count("|") >= 2 and len(line.split()) < 4:
        return ""
    return line


def _looks_like_metadata_only(line: str) -> bool:
    if not line:
        return True
    lowered = line.lower()
    metadata_tokens = (
        "importance",
        "contains_figures",
        "contains_tables",
        "section_name",
        "section_type",
        "source_sections",
    )
    if any(token in lowered for token in metadata_tokens):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z-]+", line)
    return bool(words) and len(words) <= 4 and any(word.lower() in {"content", "foundation", "main", "support"} for word in words)


def _looks_like_bibliography(line: str) -> bool:
    if not line:
        return False
    has_year = bool(re.search(r"\b(?:19|20)\d{2}[a-z]?\b", line))
    has_venue = bool(
        re.search(
            r"\b(?:journal|proceedings|conference|transactions|arxiv|doi|isbn|acm|ieee|neurips|icml|iclr)\b",
            line,
            flags=re.IGNORECASE,
        )
    )
    has_many_names = len(re.findall(r"\b[A-Z][a-z]+,\s+[A-Z]\.", line)) >= 2
    return has_year and (has_venue or has_many_names)


def repair_truncated_sentence_end(line: str) -> str:
    """Remove obvious dangling endings introduced by capacity-based truncation."""
    if not isinstance(line, str) or not line:
        return line
    line = re.sub(r"[,;:]\s*\.$", ".", line.strip())
    previous = None
    while previous != line:
        previous = line
        line = re.sub(
            r"\s+(?:while|where|when|which|that|because|although|whereas)\s+[^.;:]{1,120}"
            r"\s+(?:may|can|could|would|should|will|must|is|are|was|were|be|been|being|with\s+[A-Z])\.$",
            ".",
            line,
            flags=re.IGNORECASE,
        )
        line = re.sub(
            r"[,;:]\s+[^.;:]{1,120}\s+(?:may|can|could|would|should|will|must|is|are|was|were|be|been|being|with\s+[A-Z])\.$",
            ".",
            line,
            flags=re.IGNORECASE,
        )
        line = re.sub(r"\s+as\s+(?:a|an|the)\s+[A-Za-z-]{2,28}\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(r"\s+by\s+[A-Za-z-]+ing\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(r"\s+(?:with|using|via|by|for|to)\s+[A-Z]\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(rf"\s+({DANGLING_TERMINAL_WORDS})\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(
            r"\s+and\s+(?:also|then|therefore|local|stale|limited|new|more|less|other)\.$",
            ".",
            line,
            flags=re.IGNORECASE,
        )
        line = re.sub(r"\s+instead\s+of\s+[^.;:]{1,64}\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(r"\s+by\s+first\s+[A-Za-z-]+ing(?:\s+[A-Za-z-]+){0,2}\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(r"\s+using\s+[A-Za-z-]+(?:\s+[A-Za-z-]+){0,2}\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(r",\s+(travel|local|geospatial|stale|limited)\.$", ".", line, flags=re.IGNORECASE)
        line = re.sub(r"[,;:]\s*\.$", ".", line.strip())
        line = re.sub(r"\.{2,}$", ".", line)
    return line


def _repair_leading_bold_label(line: str) -> str:
    """Turn malformed '**Label: rest' into '**Label:** rest'."""
    bullet_match = re.match(r"^([•◦]\s+)?\*\*([^*\n:]{2,48}):\s+(.*)$", line)
    if not bullet_match:
        trailing_match = re.match(r"^([•◦]\s+)?([^*\n:]{2,48}):\*\*\s+(.*)$", line)
        if not trailing_match:
            return line
        bullet = trailing_match.group(1) or ""
        label = trailing_match.group(2).strip()
        rest = trailing_match.group(3).strip()
        return f"{bullet}**{label}:** {rest}"
    bullet = bullet_match.group(1) or ""
    label = bullet_match.group(2).strip()
    rest = bullet_match.group(3).strip()
    return f"{bullet}**{label}:** {rest}"
