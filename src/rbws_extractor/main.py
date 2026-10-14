"""CLI entrypoint for RBWS FVI → Excel extraction."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rbws_extractor.config import get_settings
from rbws_extractor.logging_config import setup_logging
from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbws-extractor",
        description=(
            "Recreate manually prepared RBWS Excel workbooks from FVI PDFs using "
            "Python OCR and rule-based extraction only."
        ),
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", type=Path, help="Single input FVI PDF path")
    src.add_argument(
        "--pdf-dir",
        type=Path,
        help="Directory of PDFs to process as a batch",
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="Output RBWS Excel path (.xlsx) for single PDF mode",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/excel"),
        help="Output directory for batch mode Excel files",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("outputs/run"),
        help="Directory for page renders, JSON, and image crops",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional page limit (0 = all pages)",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Do not crop/embed Photos and ISO images",
    )
    parser.add_argument(
        "--reuse-pages",
        type=Path,
        default=None,
        help="Reuse already-rendered page_*.png from this directory",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override log level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--edop-piping",
        action="store_true",
        help=(
            "EDOP-QP process piping mode: --pdf is the summary report; "
            "--line-pdf-dir holds per-line PDFs (JV Piping List S/N filenames)."
        ),
    )
    parser.add_argument(
        "--line-pdf-dir",
        type=Path,
        default=None,
        help="Directory of per-line PDFs for --edop-piping (defaults to summary PDF dir)",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=0,
        help="Optional limit on line PDFs for --edop-piping (0 = all)",
    )
    return parser


def _collect_pdfs(pdf_dir: Path) -> list[Path]:
    pdfs = sorted(pdf_dir.glob("*.pdf")) + sorted(pdf_dir.glob("*.PDF"))
    # de-dupe while preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in pdfs:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(args.log_level or settings.log_level)

    if args.edop_piping:
        from rbws_extractor.excel.writer import RbwsExcelWriter
        from rbws_extractor.ocr.edop_piping import build_edop_result

        if not args.pdf or not args.pdf.exists():
            print(f"ERROR: summary PDF not found: {args.pdf}", file=sys.stderr)
            return 2
        if not args.output:
            print("ERROR: --output is required for --edop-piping", file=sys.stderr)
            return 2
        line_dir = args.line_pdf_dir or args.pdf.parent
        if not line_dir.exists():
            print(f"ERROR: line PDF directory not found: {line_dir}", file=sys.stderr)
            return 2
        result = build_edop_result(
            summary_pdf=args.pdf,
            line_pdf_dir=line_dir,
            work_dir=args.work_dir,
            max_lines=args.max_lines,
        )
        RbwsExcelWriter(settings.config_path).write(
            result,
            args.output,
            embed_images=not args.skip_images,
        )
        print(
            f"EDOP piping: {len(result.findings)} findings → {args.output} "
            f"(warnings={len(result.warnings)})"
        )
        for warning in result.warnings:
            print(f"  warning: {warning}")
        return 0

    pipeline = OcrPipeline(settings)

    if args.pdf_dir:
        if not args.pdf_dir.exists():
            print(f"ERROR: PDF directory not found: {args.pdf_dir}", file=sys.stderr)
            return 2
        pdfs = _collect_pdfs(args.pdf_dir)
        if not pdfs:
            print(f"ERROR: No PDFs found in {args.pdf_dir}", file=sys.stderr)
            return 2
        results = pipeline.run_batch(
            pdf_paths=pdfs,
            output_dir=args.output_dir,
            work_root=args.work_dir,
            skip_images=args.skip_images,
            max_pages=args.max_pages,
            reuse_pages_dir=args.reuse_pages,
        )
        print(f"Batch complete: {len(results)} PDF(s) → {args.output_dir}")
        for pdf, result in zip(pdfs, results):
            print(
                f"  {pdf.name}: findings={len(result.findings)} "
                f"warnings={len(result.warnings)} review={len(result.needs_review)}"
            )
        return 0

    if not args.pdf or not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 2
    if not args.output:
        print("ERROR: --output is required for single PDF mode", file=sys.stderr)
        return 2

    result = pipeline.run(
        pdf_path=args.pdf,
        output_xlsx=args.output,
        work_dir=args.work_dir,
        max_pages=args.max_pages,
        skip_images=args.skip_images,
        reuse_pages_dir=args.reuse_pages,
    )

    print(
        f"Extracted {len(result.findings)} findings → {args.output} "
        f"(warnings={len(result.warnings)}, needs_review={len(result.needs_review)})"
    )
    for warning in result.warnings:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
