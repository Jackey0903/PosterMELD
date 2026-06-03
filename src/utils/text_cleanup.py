"""Text cleanup helpers shared by layout and rendering agents."""

import re


COMMON_OCR_FIXES = {
    "Effcient": "Efficient",
    "effcient": "efficient",
    "Effciency": "Efficiency",
    "effciency": "efficiency",
}


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
        line = _repair_leading_bold_label(line)
        normalized_lines.append(line)

    return "\n".join(normalized_lines)


def _repair_leading_bold_label(line: str) -> str:
    """Turn malformed '**Label: rest' into '**Label:** rest'."""
    bullet_match = re.match(r"^([•◦]\s+)?\*\*([^*\n:]{2,48}):\s+(.*)$", line)
    if not bullet_match:
        return line
    bullet = bullet_match.group(1) or ""
    label = bullet_match.group(2).strip()
    rest = bullet_match.group(3).strip()
    return f"{bullet}**{label}:** {rest}"
