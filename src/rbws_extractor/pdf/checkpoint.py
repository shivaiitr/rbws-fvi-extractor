"""Persist and resume successful page classifications across timeouts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import ClassifiedPage, ContentMode, PageType
from rbws_extractor.pdf.renderer import RenderedPage

logger = get_logger("pdf.checkpoint")

SUCCESS_MIN_CONFIDENCE = 0.4


def is_successful_classification(page: ClassifiedPage) -> bool:
    """True when vision/heuristic classification should be kept on resume."""
    if page.confidence < SUCCESS_MIN_CONFIDENCE:
        return False
    hint = (page.section_hint or "").lower()
    if hint.startswith("classify_fallback:"):
        return False
    if page.page_type == PageType.OTHER and page.confidence < 0.5:
        return False
    return True


class ClassificationCheckpoint:
    """JSON checkpoint of successfully classified pages."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._by_page: dict[int, ClassifiedPage] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read checkpoint %s: %s", self.path, exc)
            return

        pages = raw.get("pages", []) if isinstance(raw, dict) else raw
        loaded = 0
        for item in pages:
            try:
                page = ClassifiedPage.model_validate(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping invalid checkpoint entry: %s", exc)
                continue
            if is_successful_classification(page):
                self._by_page[page.page_number] = page
                loaded += 1
        logger.info(
            "Loaded %s successful page classifications from %s",
            loaded,
            self.path,
        )

    def save(self) -> None:
        payload = {
            "version": 1,
            "successful_count": len(self._by_page),
            "pages": [
                self._by_page[n].model_dump(mode="json")
                for n in sorted(self._by_page)
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, page_number: int) -> ClassifiedPage | None:
        return self._by_page.get(page_number)

    def put(self, page: ClassifiedPage, *, persist: bool = True) -> None:
        if not is_successful_classification(page):
            return
        self._by_page[page.page_number] = page
        if persist:
            self.save()

    def merge_rendered_paths(
        self,
        rendered: Iterable[RenderedPage],
    ) -> dict[int, ClassifiedPage]:
        """
        Re-bind checkpoint records to current rendered image paths / native text.
        """
        rendered_map = {p.page_number: p for p in rendered}
        rebound: dict[int, ClassifiedPage] = {}
        for num, saved in self._by_page.items():
            current = rendered_map.get(num)
            if current is None:
                continue
            rebound[num] = saved.model_copy(
                update={
                    "image_path": current.image_path,
                    "native_text": current.native_text or saved.native_text,
                    "content_mode": current.content_mode,
                    "char_count": current.char_count,
                    "image_count": current.image_count,
                }
            )
        self._by_page = rebound
        return dict(self._by_page)

    @property
    def successful_pages(self) -> dict[int, ClassifiedPage]:
        return dict(self._by_page)


def seed_checkpoint_from_log_classifications(
    checkpoint: ClassificationCheckpoint,
    rendered: list[RenderedPage],
    classifications: list[tuple[int, str, float]],
) -> int:
    """
    Seed checkpoint from known successful (page_number, page_type, confidence).
    Used to recover progress after a timeout crash before checkpoints existed.
    """
    rendered_map = {p.page_number: p for p in rendered}
    added = 0
    for page_number, page_type, confidence in classifications:
        if page_number in checkpoint.successful_pages:
            continue
        current = rendered_map.get(page_number)
        if current is None:
            continue
        try:
            ptype = PageType(page_type)
        except ValueError:
            continue
        page = ClassifiedPage(
            page_number=page_number,
            image_path=current.image_path,
            page_type=ptype,
            confidence=confidence,
            native_text=current.native_text or None,
            content_mode=current.content_mode
            if isinstance(current.content_mode, ContentMode)
            else ContentMode(current.content_mode),
            char_count=current.char_count,
            image_count=current.image_count,
            section_hint="seeded_from_prior_run",
        )
        if is_successful_classification(page):
            checkpoint.put(page, persist=False)
            added += 1
    if added:
        checkpoint.save()
        logger.info("Seeded %s classifications into checkpoint", added)
    return added
