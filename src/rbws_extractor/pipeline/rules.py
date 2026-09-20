"""Deterministic helpers for gold-standard RBWS row rules."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from rbws_extractor.config import DEFAULT_CONFIG


@lru_cache(maxsize=4)
def load_rbws_config(path: str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_CONFIG
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def good_condition_patterns(config: dict[str, Any] | None = None) -> list[re.Pattern[str]]:
    cfg = config or load_rbws_config()
    return [re.compile(p) for p in cfg.get("good_condition_patterns", [])]


def is_good_condition_text(text: str, config: dict[str, Any] | None = None) -> bool:
    if not text:
        return False
    for pattern in good_condition_patterns(config):
        if pattern.search(text):
            return True
    return False


def normalize_whitespace(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = re.sub(r"[ \t]+", " ", text.strip())
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or None


def normalize_technical_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    text = text.replace("\u2019", '"').replace("\u2018", '"')
    text = text.replace("”", '"').replace("″", '"').replace("'", '"')
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\"\"", '"')
    text = re.sub(r"(?i)No\s+Tag\s+found", "No Tag found", text)
    text = re.sub(r"(?i)\bNGR1-AH-\s*PDPID-(\d{7})\b", r"NGR1-AH-PDPID-\1", text)
    text = re.sub(
        r'(?i)\bWF-(\d{1,5})-(\d{1,2})\s*["”″\']?\s*-\s*CBR-?(\d+)\b',
        r'WF-\1-\2"-CBR-\3',
        text,
    )
    text = re.sub(
        r'(?i)\bWF-(\d{1,5})-(\d{1,2})\s*["”″\']?\s*-\s*(\d{1,2})\s*-\s*FAA-?(\d+)\b',
        r'WF-\1-\2"-\3-FAA-\4',
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace('" -', '"-').replace('" -', '"-')
    return text or None


def join_from_to(from_text: str | None, to_text: str | None) -> str | None:
    left = normalize_whitespace(from_text)
    right = normalize_whitespace(to_text)
    if left and right:
        if left.lower().endswith(" to") or right.lower().startswith("to "):
            return normalize_whitespace(f"{left} {right}")
        return f"{left} to {right}"
    return left or right


def default_line_number_for_class(asset_class: str) -> str:
    """Fallback only when no Equipment Tag value is present in the source PDF.

    Per the business rule, the Excel Line # must come from the PDF's Equipment Tag
    field and must never be populated with the section or equipment type name.
    """
    return "No Tag found"


def should_blank_pid(asset_class: str, config: dict[str, Any] | None = None) -> bool:
    cfg = config or load_rbws_config()
    blank_for = cfg.get("row_rules", {}).get("blank_pid_for_asset_classes", [])
    return asset_class in blank_for


CYLINDER_KEYWORDS = (
    "cylinder",
    "afff",
    "fire protection",
)
PUMP_KEYWORDS = ("pump", "caisson")
HOSE_KEYWORDS = ("hose reel", "hose-reel")
MONITOR_KEYWORDS = ("monitor",)
DIESEL_KEYWORDS = ("diesel", "tank")
PIPING_KEYWORDS = ("wf-", "line", "header", "deluge", "piping")


def infer_asset_class_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in CYLINDER_KEYWORDS):
        return "fire_protection_cylinder"
    if any(k in t for k in PUMP_KEYWORDS):
        return "fire_water_pump_caisson"
    if any(k in t for k in HOSE_KEYWORDS):
        return "fire_water_hose_reel"
    if any(k in t for k in MONITOR_KEYWORDS):
        return "fire_water_monitor"
    if any(k in t for k in DIESEL_KEYWORDS):
        return "diesel_storage_tank"
    if any(k in t for k in PIPING_KEYWORDS):
        return "fire_water_piping"
    return "unknown"