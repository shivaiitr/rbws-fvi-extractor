"""OCR-only end-to-end pipeline (Python + Tesseract, no LLM)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from rbws_extractor.config import Settings, get_settings
from rbws_extractor.excel.writer import RbwsExcelWriter
from rbws_extractor.logging_config import get_logger, setup_logging
from rbws_extractor.models.findings import ClassifiedPage, ExtractionResult, PageType
from rbws_extractor.models.schemas import write_schemas
from rbws_extractor.ocr.engine import OcrEngine
from rbws_extractor.ocr.parsers import OcrReportParser
from rbws_extractor.ocr.jimcel_native import parse_jimcel_native
from rbws_extractor.ocr.crop import attach_images_from_ocr, enrich_anomaly_from_captions
from rbws_extractor.pipeline.validate import validate_result

logger = get_logger("pipeline.ocr")


class OcrPipeline:
    """PDF → OCR → rule parsers → Excel."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        setup_logging(self.settings.log_level)
        self.engine = OcrEngine(dpi=self.settings.render_dpi)
        self.parser = OcrReportParser()
        self.excel_writer = RbwsExcelWriter(self.settings.config_path)

    def run_batch(
        self,
        pdf_paths: list[Path],
        output_dir: Path,
        work_root: Path,
        skip_images: bool = False,
        max_pages: int = 0,
        reuse_pages_dir: Path | None = None,
    ) -> list[ExtractionResult]:
        output_dir = Path(output_dir)
        work_root = Path(work_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        results: list[ExtractionResult] = []
        for pdf_path in pdf_paths:
            pdf_name = Path(pdf_path).stem
            results.append(
                self.run(
                    pdf_path=pdf_path,
                    output_xlsx=output_dir / f"{pdf_name}.xlsx",
                    work_dir=work_root / pdf_name,
                    max_pages=max_pages,
                    skip_images=skip_images,
                    reuse_pages_dir=reuse_pages_dir,
                )
            )
        return results

    def run(
        self,
        pdf_path: Path,
        output_xlsx: Path,
        work_dir: Path,
        max_pages: int = 0,
        skip_images: bool = False,
        reuse_pages_dir: Path | None = None,
    ) -> ExtractionResult:
        pdf_path = Path(pdf_path)
        output_xlsx = Path(output_xlsx)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        pages_dir = work_dir / "pages"
        text_dir = work_dir / "ocr_text"
        artifacts_dir = work_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Starting OCR-only RBWS extraction for %s", pdf_path)

        if reuse_pages_dir and Path(reuse_pages_dir).exists():
            src = Path(reuse_pages_dir)
            pages_dir.mkdir(parents=True, exist_ok=True)
            for png in sorted(src.glob("page_*.png")):
                dest = pages_dir / png.name
                if not dest.exists():
                    shutil.copy2(png, dest)
            logger.info("Copied page renders from %s", src)

        rendered = self.engine.ensure_pages(pdf_path, pages_dir, max_pages=max_pages)
        native_dir = work_dir / "native_text"
        texts = self.engine.extract_native_text(
            pdf_path, native_dir, max_pages=max_pages
        )
        text_source = "native"
        if texts is None:
            texts = self.engine.ocr_pages(rendered, text_dir)
            text_source = "ocr"

        parsed = None
        if text_source == "native":
            parsed = parse_jimcel_native(texts)
        if parsed is not None:
            metadata, findings = parsed
            logger.info("Used Jimcel native/table parsers")
        else:
            metadata, findings = self.parser.parse(texts)
            logger.info("Used OCR heuristic parsers")

        findings = enrich_anomaly_from_captions(findings, texts)
        findings = attach_images_from_ocr(
            findings,
            texts=texts,
            pages_dir=pages_dir,
            output_dir=artifacts_dir,
            skip_images=skip_images,
            pdf_path=pdf_path,
        )

        classified = [
            ClassifiedPage(
                page_number=n,
                image_path=next(
                    (p.image_path for p in rendered if p.page_number == n),
                    pages_dir / f"page_{n:03d}.png",
                ),
                page_type=PageType.OTHER,
                section_hint="ocr",
                confidence=1.0,
                text_preview=texts.get(n, "")[:500],
                native_text=texts.get(n, "")[:2000],
            )
            for n in sorted(texts)
        ]

        photos_n = sum(1 for f in findings if f.photos_image_path)
        iso_n = sum(1 for f in findings if f.iso_image_path)
        result = ExtractionResult(
            metadata=metadata,
            findings=findings,
            classified_pages=classified,
            source_pdf=pdf_path,
            warnings=[
                "engine=ocr",
                "llm=disabled",
                f"text_source={text_source}",
                f"pages={len(texts)}",
                (
                    f"images=photos:{photos_n}/iso:{iso_n}"
                    if not skip_images
                    else "images=skipped"
                ),
            ],
        )
        result = validate_result(result)

        (work_dir / "extraction_result.json").write_text(
            result.model_dump_json(indent=2),
            encoding="utf-8",
        )
        write_schemas(work_dir / "schemas")
        self.excel_writer.write(result, output_xlsx, embed_images=not skip_images)

        logger.info(
            "OCR done. findings=%s warnings=%s output=%s",
            len(result.findings),
            len(result.warnings),
            output_xlsx,
        )
        return result
