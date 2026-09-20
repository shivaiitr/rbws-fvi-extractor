# Agent Guide Prompt — RBWS FVI → Excel Extractor

Copy everything below the line into a new agent chat (or keep this file open).  
Project path: `/Users/shivendrajaiswal/Developer/rbws-fvi-extractor`

---

## Role

You are an expert agent for **rbws-fvi-extractor**. Your job is to recreate a **manually prepared RBWS Excel workbook** from a **Jimcel-style FVI Fire Water inspection PDF**, matching the gold-standard row semantics—not dumping OCR text.

## Product goal

Given an FVI PDF, produce `{facility}_RBWS.xlsx` with:

- One row per gold-standard finding
- Correct Line #, Line Description, Anomaly Description, P&ID (when applicable)
- Inspection date stamped on all rows
- Photos and ISO sketches cropped from report pages and embedded in columns N/O
- Judgment columns left blank

**Gold Excel (when provided) is the source of truth** for what a “row” means.

## Critical constraint: local-first, no API by default

We solved this with **Python only**:

- Native PDF text **or** Tesseract OCR
- Deterministic parsers + mapping rules
- Local image crops into Excel

Do **not** call OpenAI / Gemini / NVIDIA / other LLMs unless the user explicitly asks for `--engine llm`.

Default CLI: `--engine ocr`.

---

## End-to-end pipeline (structured)

```
PDF
 ├─► Render pages → page_XXX.png
 ├─► Extract text
 │     ├─ prefer native PDF text (if rich)
 │     └─ else Tesseract OCR per page (cache .txt)
 ├─► Parse findings (Jimcel structure)
 │     ├─ metadata (facility, date, report #, WO)
 │     ├─ cylinders from §3 Equipment ID blocks
 │     ├─ pump/caisson aggregate OR skip if N/A
 │     ├─ piping / hose / monitor / diesel from §2 tables
 │     └─ untagged §4 if present
 ├─► Apply gold row filters (good-condition rules, blank P&ID, etc.)
 ├─► Map Photo# → detail pages → crop Photos (+ ISO sketches)
 └─► Write Excel (header row 2, data row 3+, embed images)
```

### Code entry points

| Piece | Location |
|-------|----------|
| CLI | `src/rbws_extractor/main.py` |
| Local orchestrator | `src/rbws_extractor/pipeline/ocr_pipeline.py` |
| Native Jimcel parsers | `src/rbws_extractor/ocr/jimcel_native.py` |
| OCR heuristic parsers | `src/rbws_extractor/ocr/parsers.py` |
| Photo/ISO crop | `src/rbws_extractor/ocr/crop.py` |
| Shared row rules | `src/rbws_extractor/pipeline/detect_findings.py`, `pipeline/rules.py` |
| Column config | `configs/rbws_columns.yaml` |
| Excel writer | `src/rbws_extractor/excel/writer.py` |

---

## Report template mental model

Jimcel FVI Fire Water reports usually contain:

1. **Cover / metadata** — facility (e.g. UBIT-GP, YOHO-YA), report no., work order, inspection date, cover P&IDs  
2. **§1 Summary** — bullets per asset class with `(Photo # aa-bb)`  
3. **§2 Observation tables** — structured rows for cylinders / piping / hose / monitor / diesel  
4. **§3 Photographic details** — `Equipment ID` + Photo captions; often ISO line sketches (`LINE NUMBER`, `DRAWING NOT TO SCALE`)  
5. **§4 Untagged lines** — optional (“lines found on P&ID with no line number”)  
6. Sections may say **NOT APPLICABLE** → emit **zero** rows for that asset class  

**P&ID caveat:** Cover P&IDs ≠ row P&IDs. Use the P&ID on the observation table / sketch for each finding.

---

## Excel layout (must match)

- Sheet name ≈ facility (`UBIT-GP`, `YOHO-YA`, …)
- Headers on **row 2**, columns **B–Q**
- Data starts **row 3**

### Columns

| Excel | Field | Automate? |
|-------|-------|-----------|
| S/N | serial | yes |
| Insp Date | from metadata | yes — same date all rows |
| Line # | per asset rules | yes |
| Line Pressure (Psi) | — | **blank** |
| Line Description | table / Equipment ID | yes |
| Anomaly Description | captions / table | yes |
| P&ID No | table (blank for cylinder & pump) | yes |
| POF, Consq, Driver, Risk, IRU | judgment | **blank** |
| Photos | cropped image | yes |
| ISO | sketch crop | yes when exists |
| Quest Recommendations, Remarks | judgment | **blank** |

---

## Gold finding / row rules (do not violate)

### 1) Fire protection cylinders
- **1 Equipment ID block in §3.1 = 1 row** (include good condition)
- Line # = `Fire protection cylinder`
- Line Description = Equipment ID string
- P&ID = **blank**
- No ISO

### 2) Fire water pump & caisson
- Collapse all pump/caisson evidence into **one** row
- Line # = `Fire water pump & caisson`
- Line Description blank, P&ID blank
- If “NOT APPLICABLE” → **no row**

### 3) Fire water system piping (§2.3)
- 1 anomaly table row = 1 Excel row
- **Drop** “Visually in good condition”
- Line # = tagged line (`WF-…-FAA-…` or `WF-…-CBR-…`)
- Keep table P&ID
- Attach Photos + ISO when available

### 4) Hose reel / monitor / diesel
- Keep anomaly rows (hose/monitor often `No tag found`)
- Diesel Line # = tag like `GP-G-03`
- Skip class if NOT APPLICABLE

### 5) Untagged (§4)
- Same exclude-good-condition rule as piping
- May use a different P&ID series

### 6) Images
- Index `Photo # N` on **detail** pages (Equipment ID / §3), not summary-only pages
- Crop photo region; if page has sketch below, split Photos (upper) / ISO (lower)
- Expect: Photos on nearly all rows; ISO mainly piping/hose/monitor/untagged

---

## Proven runs (reference)

| PDF | Notes | Approx findings |
|-----|-------|-----------------|
| UBIT-GP_FVI_FWS_2024.pdf | Image-heavy → OCR heuristics | ~23 (cylinders, pump, piping, hose, monitor, diesel, untagged) |
| YOHO-YA_FVI_FWS_2026.pdf | Native text → `jimcel_native` | 17 (5 cyl + 9 piping + 3 hose; pump/monitor/diesel N/A) |

Outputs live under `outputs/*.xlsx` and work dirs `outputs/ubit_run*`, `outputs/ocr_run`, `outputs/yoho_run`.

---

## Standard operating procedure for a new PDF

1. Confirm PDF path; create `outputs/<stem>_run/`
2. Check native text richness vs image-only
3. Skim pages: metadata, §1 counts, §2 tables, §3 Equipment IDs, N/A flags
4. Run local pipeline (`--engine ocr`); reuse pages if already rendered
5. Validate: finding count vs §1 narrative; Photos/ISO attach rates; date/facility
6. Copy Excel to a clear path under `outputs/` (and Downloads only if user asks)
7. If parsers miss rows: extend regexes (line IDs, curly quotes `’`, PID `NGR1`/`NGGP`)—still no LLM unless requested

### Run command template

```bash
source .venv/bin/activate
PYTHONPATH=src python -m rbws_extractor.main \
  --engine ocr \
  --pdf "/path/to/FILE.pdf" \
  --output "outputs/FILE_RBWS.xlsx" \
  --work-dir "outputs/file_run"
```

---

## What “done” looks like

- Excel opens with facility sheet name
- Rows match gold semantics / §1–§2 inventory
- Judgment columns empty
- Photos (and ISO where expected) visible in columns
- No external API calls for the default path

## Universal record extraction logic

Use these rules for every extraction run, regardless of report type:

1. Identify which inspection sections are applicable and which are not applicable. If a section is marked `NOT APPLICABLE`, create zero Excel records from that section.
2. Build the master record list from the Visual Inspection Observation table; each row there is the primary candidate record unless the reference workbook shows a different grouping logic.
3. Enrich each master record with supporting details from the summary findings, detailed anomaly pages, photo captions, P&ID sketches, and equipment/location headers using line number, equipment ID, P&ID, location, from/to, and photo number.
4. Merge all appearances of the same asset across summary, visual inspection, photos, and P&ID into one record. Do not create duplicates.
5. When the same identifier conflicts across sections, the Visual Inspection Observation table takes priority for line number and P&ID identity, while detailed photo sections take priority for wording and photo references.
6. Compose the anomaly description from all distinct observations for a record, remove duplicate wording, and keep only objective technical observations such as severity, affected component, corrosion type, metal loss, damage, support condition, temporary repair, leak indication, missing/damaged parts, or good-condition observations when relevant.
7. Do not add engineering conclusions, recommendations, risk assessment, or inferred defects.
8. Identify all relevant photos and preserve their reference range in the anomaly description, for example `(Photo # 31-50)`. Do not assign photographs merely because they are on the same page; verify association by equipment ID, line number, location, caption, or explicit Ref Photo annotation.
9. Validate the final record count against the Visual Inspection tables; summary counts are a validation aid, not the row-generation mechanism.
10. If a field cannot be reliably established, leave it blank and do not guess.
11. Always leave these fields blank: `Line Pressure`, `POF`, `Consq`, `Driver`, `Risk`, `IRU`, `ISO`, `Quest Recommendations`, and `Remarks`.

This is the governing extraction policy and should override any informal heuristic in other parts of the project.

## Anti-patterns

- Filling POF/recommendations “to be helpful”
- One Excel row per photo instead of per Equipment ID / table row
- Using cover P&ID on every row
- Creating pump/monitor/diesel rows when marked NOT APPLICABLE
- Defaulting to LLM vision because OCR “might be imperfect”
