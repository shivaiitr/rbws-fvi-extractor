# RBWS FVI Extractor

Local Python tool: **FVI Fire Water inspection PDF → RBWS Excel**.

No LLM / API. Native PDF text when available, otherwise Tesseract OCR, then deterministic parsers.

## Flow

```
PDF
  → render pages to PNG
  → extract text (native PDF text, else Tesseract)
  → parse Jimcel sections into findings
  → crop photo / sketch evidence
  → write Excel (header row 2, data from row 3)
```

| Step | Code |
|------|------|
| CLI | `src/rbws_extractor/main.py` |
| Orchestrate | `src/rbws_extractor/pipeline/ocr_pipeline.py` |
| Text | `src/rbws_extractor/ocr/engine.py` |
| Native tables | `src/rbws_extractor/ocr/jimcel_native.py` |
| OCR fallback | `src/rbws_extractor/ocr/parsers.py` |
| Images | `src/rbws_extractor/ocr/crop.py` |
| Excel | `src/rbws_extractor/excel/writer.py` |
| Columns | `configs/rbws_columns.yaml` |

## What gets filled vs left blank

**Filled:** S/N, Insp Date, Line #, Line Description, Anomaly Description, P&ID No, Photos (and ISO only if the writer still attaches a sketch).

**Always blank (engineering judgment):** Line Pressure, POF, Consq, Driver, Risk, IRU, Quest Recommendations, Remarks.

## How rows are created

1. Honor **NOT APPLICABLE** — zero rows for that section.
2. Prefer **Visual Inspection Observation tables** as the master list.
3. One inspection item = one Excel row (many photos still one row).
4. Skip “visually in good condition” piping / untagged rows.
5. Pump/caisson: one aggregated row, or none if N/A.
6. Do not invent tags. Keep `No Tag found` when the table says that.

## Run

```bash
cd ~/Developer/rbws-fvi-extractor
source .venv/bin/activate
pip install -r requirements.txt

PYTHONPATH=src python -m rbws_extractor.main \
  --pdf /path/to/REPORT.pdf \
  --output outputs/REPORT_RBWS.xlsx \
  --work-dir outputs/report_run
```

## Team UI

Streamlit app with **two workflows** (see `app.py`):

| | Workflow A | Workflow B |
|---|------------|------------|
| Use for | Fire Water FVI (e.g. UBIT-GP) | Process Piping (e.g. EDOP-QP) |
| Upload | One inspection PDF | Summary PDF + all line-number PDFs |

Local:

```bash
export PYTHONPATH=src
export APP_PASSWORD='choose-a-password'
streamlit run app.py
```

Deploy: prefer **Railway** (Docker + Tesseract + large uploads). **Vercel is not suitable.** Steps in [docs/DEPLOY.md](docs/DEPLOY.md).

GitHub: https://github.com/shivaiitr/rbws-fvi-extractor (source only — not the live URL).

Do not host confidential inspection PDFs on a public app without a password and company approval.
