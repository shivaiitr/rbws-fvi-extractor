# RBWS FVI Extractor

Production Python application that recreates a manually prepared **RBWS Excel**
workbook from an image-based FVI (Focused Visual Inspection) PDF report.

This project is built around a Python OCR + rule-based extraction pipeline and
runs without any external LLM API dependency. It reverse-engineers the engineer
workflow used for `UBIT-GP_FVI_FWS_2024`:

1. Read metadata (inspection date, facility)
2. Create one row per cylinder Equipment ID block (including good condition)
3. Aggregate pump/caisson into one row
4. Create rows from §2 / §4 observation tables (anomaly rows only for piping)
5. Merge observations + photo references
6. Crop Photos / ISO images from the report
7. Leave POF / recommendations / risk columns blank for engineering judgment

## Setup

```bash
cd ~/Developer/rbws-fvi-extractor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Python-only execution

This repo is intended to run without any LLM provider. The default path is the
OCR + rule parsing pipeline:

```bash
cd ~/Developer/rbws-fvi-extractor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Mixed PDF support (text + scanned)

The extractor auto-detects each page:

| Page type | What happens |
|---|---|
| Native text PDF page | Uses extracted text (cheaper/faster) |
| Image-only / scanned page | Uses vision model |
| Hybrid (text + photo grids) | Uses text + images |

For many PDFs in one folder:

```bash
python -m rbws_extractor.main \
  --pdf-dir /path/to/pdfs \
  --output-dir outputs/excel \
  --work-dir outputs/runs \
  --mode auto
```

Override mode if needed: `--mode text` or `--mode vision`.

## Architecture

```
PDF → Page Render → Page Classify → Finding Detect
    → Field Extract → Merge → Crop Photos/ISO
    → Validate → Excel Writer
```

Judgment fields (`POF`, `Quest Recommendations`, consequence/risk columns)
remain blank by design so integrity engineers complete them.

## Universal record extraction logic

The extraction follows the gold-standard row logic below for every report:

1. Identify applicable sections and mark non-applicable sections as zero-row sections.
2. Use the Visual Inspection Observation table as the primary master list for candidate rows.
3. Enrich each candidate row with supporting details from summary findings, detailed anomaly pages, photo captions, P&ID sketches, and equipment/location headers.
4. Treat the same asset appearing in multiple sections as a single record; do not create duplicates.
5. If identifiers conflict, the Visual Inspection Observation table wins for line/P&ID identity, while detailed photo sections win for anomaly wording and photo references.
6. Build the final anomaly description by merging distinct observations only, removing duplicate wording, and preserving relevant technical detail without adding engineering conclusions or recommendations.
7. Preserve photo references in anomaly text when supported by equipment ID, line number, location, caption, or explicit Ref Photo annotation.
8. Validate the final record count against the visual inspection tables; summary counts are a validation aid, not the primary row-generation driver.
9. Leave uncertain values blank rather than guessing.
10. Always keep the engineering judgment fields blank: `Line Pressure`, `POF`, `Consq`, `Driver`, `Risk`, `IRU`, `ISO`, `Quest Recommendations`, and `Remarks`.

This rule set is enforced in the project’s validation logic and the Excel writer template.

## Tech stack

Python 3.11+, PyMuPDF, OpenAI multimodal API, Pandas, OpenPyXL, Pillow,
Pydantic, python-dotenv, tqdm.
