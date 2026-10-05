# Agent guide — how this extractor actually works

Project: `/Users/shivendrajaiswal/Developer/rbws-fvi-extractor`

Default path is **local only**. Do not call OpenAI / Gemini / NVIDIA.

---

## Job

Turn a Jimcel-style FVI Fire Water PDF into an RBWS Excel that looks like the gold workbook — not a raw OCR dump.

## Pipeline (code path)

```
PDF
 ├─ render pages → page_XXX.png          (PdfRenderer)
 ├─ text
 │    ├─ native PDF text if rich enough  (OcrEngine.extract_native_text)
 │    └─ else Tesseract per page         (OcrEngine.ocr_pages)
 ├─ parse
 │    ├─ jimcel_native if text is clean
 │    └─ ocr/parsers.py if image-only
 ├─ crop Photos (and sketches when used)
 └─ Excel writer  (row 2 headers, data from row 3, cols B–Q)
```

Entry: `python -m rbws_extractor.main --pdf … --output … --work-dir …`

## PDF layout (Jimcel)

1. Cover / metadata — facility, date of inspection, report #, WO  
2. §1 Summary — bullets + photo ranges (validation only)  
3. §2 Visual Inspection Observation tables — **master rows**  
4. §3 Photo pages — evidence crops; Equipment ID / captions  
5. §4 Untagged lines — optional  
6. **NOT APPLICABLE** → no rows for that class  

Cover P&IDs often differ from table P&IDs. Use the **table / row** P&ID.

## Row rules

| Asset | Line # | Notes |
|-------|--------|--------|
| Piping | tagged `WF-…` | From–To in Line Description. Drop good-condition rows. |
| Equipment (cyl / hose / monitor) | **Equipment Tag** as written, including `No Tag found` | Description from the observation table. |
| Pump / caisson | one row `Fire water pump & caisson` | Skip if N/A. |
| Untagged §4 | as in table (`6" line`, etc.) | Drop good-condition. |

One table item = one Excel row even if photos span many pages.

Anomaly text: table wording + one photo range `(Photo #21-30)`, not a caption list.

## Excel columns

Fill: S/N, Insp Date, Line #, Line Description, Anomaly Description, P&ID No, Photos.

Leave blank: Line Pressure, POF, Consq, Driver, Risk, IRU, Quest Recommendations, Remarks.

Do not invent engineering values.

## New PDF checklist

1. Page count; native text vs scanned  
2. Read metadata + §2 tables + N/A flags  
3. Run local CLI  
4. Check row count vs observation tables  
5. Check Photos attach; keep judgment columns empty  

## Proven reports

- UBIT-GP (image PDF → OCR parsers)  
- YOHO-YA (native text → jimcel_native; pump/monitor/diesel N/A)
