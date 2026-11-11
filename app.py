"""Quest Global RBWS FVI Extractor — Streamlit UI for two report workflows."""

from __future__ import annotations

import base64
import html
import io
import os
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from rbws_extractor.excel.writer import RbwsExcelWriter
from rbws_extractor.ocr.edop_piping import build_edop_result, detect_pdf_kind
from rbws_extractor.ocr.vessel import build_vessel_result
from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline
from rbws_extractor.web_download import ensure_download_route, publish_workbook

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "quest-global-logo.svg"

# Quest Global brand: Pantone 511 C / #662a53 and Pantone 116 C / #ffd100.
QG_CSS = """
<style>
html, body, [class*="css"] {
    font-family: Calibri, "Segoe UI", Helvetica, Arial, sans-serif;
}
.stApp {
    background: #f4f1f2;
    color: #241820;
}
header[data-testid="stHeader"] {
    background: transparent;
    height: 0;
}
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] {
    visibility: hidden;
    height: 0;
}
[data-testid="stMainBlockContainer"],
[data-testid="stAppViewContainer"] .main .block-container {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
    max-width: none;
}
[data-testid="stMain"] .stElementContainer {
    padding-left: 2rem;
    padding-right: 2rem;
    box-sizing: border-box;
}
[data-testid="stMain"] .stElementContainer:has(.mast),
[data-testid="stMain"] .stElementContainer:has(.qg-footer) {
    padding-left: 0;
    padding-right: 0;
}
[data-testid="stSidebar"] {
    background: #f7f4f5;
    border-right: 1px solid #e3d7de;
}
[data-testid="stSidebar"] > div:first-child {
    border-top: 4px solid #662a53;
}
.mast {
    display: grid;
    grid-template-columns: 220px 1fr auto;
    gap: 1.5rem;
    align-items: center;
    margin: 0;
    padding: 0.95rem 2rem 1rem;
    background: #662a53;
    color: #fff;
    border-bottom: 4px solid #ffd100;
}
.qg-logo {
    display: block;
    width: 196px;
    height: auto;
    background: #fff;
    padding: 0.45rem 0.55rem;
}
.mast-kicker {
    margin: 0;
    color: #ffd100;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.mast h1 {
    margin: 0.15rem 0 0;
    color: #fff;
    font-size: 1.65rem;
    font-weight: 700;
    letter-spacing: 0.01em;
    line-height: 1.15;
}
.mast-sub {
    margin: 0.35rem 0 0;
    color: rgba(255,255,255,0.88);
    font-size: 0.95rem;
}
.mast-meta {
    text-align: right;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    line-height: 1.55;
    color: rgba(255,255,255,0.78);
}
.mast-meta strong {
    display: block;
    color: #ffd100;
    font-weight: 700;
}
.workflow-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 0;
    margin: 0 0 0.35rem;
    background: #fff;
    border-bottom: 1px solid #e3d7de;
}
.workflow-card {
    padding: 1rem 1.25rem 1.15rem;
    border-right: 1px solid #efe6ea;
}
.workflow-card:last-child { border-right: 0; }
.workflow-card .idx {
    display: inline-block;
    color: #ffd100;
    background: #662a53;
    padding: 0.15rem 0.45rem;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    margin-bottom: 0.45rem;
}
.workflow-card strong {
    display: block;
    color: #662a53;
    font-size: 0.95rem;
    letter-spacing: 0.02em;
    margin-bottom: 0.35rem;
}
.workflow-card p {
    margin: 0;
    color: #4a3d44;
    font-size: 0.88rem;
    line-height: 1.4;
}
.workflow-card ul {
    margin: 0.45rem 0 0;
    padding-left: 1.1rem;
    color: #4a3d44;
    font-size: 0.84rem;
    line-height: 1.4;
}
.panel-label {
    margin: 0.2rem 0 0.15rem;
    color: #662a53;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.work-title {
    margin: 0 0 0.35rem;
    color: #241820;
    font-size: 1.25rem;
    font-weight: 700;
}
.status-line {
    margin: 0.35rem 0 0.8rem;
    padding: 0.55rem 0.75rem;
    background: #fff;
    border: 1px solid #e3d7de;
    border-left: 4px solid #662a53;
    color: #241820;
    font-size: 0.92rem;
}
.status-line em {
    font-style: normal;
    color: #662a53;
    font-weight: 700;
    letter-spacing: 0.06em;
    font-size: 0.75rem;
    text-transform: uppercase;
}
.side-label {
    margin: 0 0 0.75rem;
    color: #662a53;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.side-note {
    margin-top: 0.8rem;
    color: #5c4e55;
    font-size: 0.82rem;
    line-height: 1.4;
}
.qg-footer {
    margin: 1.75rem 0 0;
    padding: 0.7rem 2rem;
    background: #662a53;
    color: rgba(255,255,255,0.88);
    font-size: 0.82rem;
    letter-spacing: 0.04em;
    border-top: 3px solid #ffd100;
}
.qg-footer strong { color: #ffd100; font-weight: 700; }
.job {
    display: flex;
    align-items: center;
    gap: 1.15rem;
    margin: 0.85rem 0 1rem;
    padding: 0.95rem 1.15rem;
    background: #fff;
    border: 1px solid #e3d7de;
    border-left: 4px solid #662a53;
}
.job.is-done { border-left-color: #ffd100; }
.job-ring {
    position: relative;
    width: 92px;
    height: 92px;
    flex: 0 0 auto;
}
.job-ring svg {
    width: 92px;
    height: 92px;
    transform: rotate(-90deg);
}
.job-track { fill: none; stroke: #efe6ea; stroke-width: 8; }
.job-value { fill: none; stroke: #662a53; stroke-width: 8; }
.job.is-done .job-value { stroke: #662a53; }
.job-ring span {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #662a53;
    font-weight: 700;
    font-size: 1.05rem;
}
.job-kicker {
    margin: 0;
    color: #662a53;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
}
.job-label {
    margin: 0.28rem 0 0;
    color: #241820;
    font-size: 1rem;
    line-height: 1.35;
}
.spec-bar {
    margin: 0 0 1.15rem;
    padding: 0.45rem 2rem;
    background: #f7f4f5;
    border-bottom: 1px solid #e3d7de;
    color: #5c4e55;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
.spec-bar strong { color: #662a53; font-weight: 700; }
div[data-testid="stWidgetLabel"] p,
div[data-testid="stFileUploader"] label p {
    color: #662a53;
    font-weight: 700;
    letter-spacing: 0.04em;
    font-size: 0.84rem;
}
div[data-testid="stRadio"] label[data-baseweb="radio"] {
    color: #241820;
}
div[data-testid="stFileUploader"] section {
    background: #fff;
    border: 1px solid #d9ccd2;
    border-radius: 0;
    padding: 0.4rem;
}
div[data-testid="stFileUploader"] section:hover {
    border-color: #662a53;
}
[data-testid="stAlert"] {
    background: #fff;
    border: 1px solid #e3d7de;
    border-left: 4px solid #ffd100;
    border-radius: 0;
    color: #241820;
}
[data-testid="stAlert"] p,
[data-testid="stAlert"] div {
    color: #241820 !important;
}
.stButton > button,
.stDownloadButton > button,
button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-secondary"] {
    border-radius: 0;
    letter-spacing: 0.04em;
    font-weight: 700;
    min-height: 2.7rem;
}
.stButton > button[kind="primary"],
button[data-testid="stBaseButton-primary"] {
    background: #662a53;
    color: #fff;
    border: 1px solid #662a53;
}
.stButton > button[kind="primary"]:hover,
button[data-testid="stBaseButton-primary"]:hover {
    background: #4f2140;
    color: #fff;
    border-color: #4f2140;
}
.stDownloadButton > button {
    background: #ffd100;
    color: #662a53;
    border: 1px solid #e6bc00;
}
@media (max-width: 1100px) {
    .workflow-grid { grid-template-columns: 1fr; }
}
@media (max-width: 900px) {
    .mast, .workflow-grid { grid-template-columns: 1fr; }
    .mast-meta { text-align: left; }
    .workflow-card { border-right: 0; border-bottom: 1px solid #efe6ea; }
    [data-testid="stMain"] .stElementContainer {
        padding-left: 1rem;
        padding-right: 1rem;
    }
}
</style>
"""

WORKFLOW_AUTO = "Automatic classification"
WORKFLOW_FIRE_WATER = "Procedure A · Fire water FVI"
WORKFLOW_EDOP = "Procedure B · Process piping"
# Separate from Auto / A / B. Rename this string to rename the flow in the UI.
WORKFLOW_VESSEL = "Pressure vessel workflow"


def _logo_html() -> str:
    if not LOGO_PATH.exists():
        return ""
    encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return (
        f'<img class="qg-logo" src="data:image/svg+xml;base64,{encoded}" '
        'alt="Quest Global" width="210">'
    )


def _render_mast() -> None:
    st.markdown(QG_CSS, unsafe_allow_html=True)
    st.markdown(
        f"""
        <header class="mast">
            <div>{_logo_html()}</div>
            <div>
                <p class="mast-kicker">Quest Global Engineering Services Limited</p>
                <h1>RBWS FVI Workbook Generator</h1>
                <p class="mast-sub">Full visual inspection reports issued as the standard RBWS workbook. Rules-based extraction. No external language models.</p>
            </div>
            <div class="mast-meta">
                <strong>Procedures</strong>
                A · Fire water
                <strong>B · C</strong>
                Piping · Vessels
                <strong>Built by</strong>
                Shivendra Jaiswal
            </div>
        </header>
        """,
        unsafe_allow_html=True,
    )


def _render_procedures() -> None:
    st.markdown(
        """
        <section class="workflow-grid">
            <div class="workflow-card">
                <span class="idx">A</span>
                <strong>Fire water systems</strong>
                <p>One inspection report. Each observation is one workbook row.</p>
                <ul>
                    <li>Visual inspection tables are the master list</li>
                    <li>Photographs and isometric sketches are placed in the sheet</li>
                    <li>Judgment columns remain blank for engineering review</li>
                </ul>
            </div>
            <div class="workflow-card">
                <span class="idx">B</span>
                <strong>Process piping</strong>
                <p>Summary checklist, plus one document per line or a single archive.</p>
                <ul>
                    <li>Line description is taken from the checklist From–To text</li>
                    <li>Anomaly text and photographs are taken from each line document</li>
                    <li>Uncertain classification is handled by automatic selection</li>
                </ul>
            </div>
            <div class="workflow-card">
                <span class="idx">C</span>
                <strong>Pressure vessels and tanks</strong>
                <p>One or more vessel reports, from any facility.</p>
                <ul>
                    <li>Summary of findings is the master equipment list</li>
                    <li>Each photograph keeps the caption printed beneath it</li>
                    <li>Probability of failure and recommendations remain blank</li>
                </ul>
            </div>
        </section>
        <p class="spec-bar"><strong>Deliverable</strong> · RBWS workbook · Source photograph resolution retained · Judgment fields left blank</p>
        """,
        unsafe_allow_html=True,
    )


def _render_chrome() -> None:
    _render_mast()
    _render_procedures()


def _render_footer() -> None:
    st.markdown(
        '<p class="qg-footer">Quest Global Engineering Services Limited · <strong>RBWS FVI Workbook Generator</strong> · Built by Shivendra Jaiswal</p>',
        unsafe_allow_html=True,
    )


def _ring_html(pct: int, label: str, *, done: bool = False) -> str:
    pct = max(0, min(100, int(pct)))
    radius = 42
    circ = 2 * 3.14159265 * radius
    offset = circ * (1 - pct / 100)
    state = " is-done" if done or pct >= 100 else ""
    kicker = "Workbook complete" if done or pct >= 100 else "Job in progress"
    safe_label = html.escape(label)
    return (
        f'<div class="job{state}">'
        f'<div class="job-ring" role="progressbar" aria-valuenow="{pct}" '
        f'aria-valuemin="0" aria-valuemax="100" aria-label="{safe_label}">'
        f'<svg viewBox="0 0 100 100" aria-hidden="true">'
        f'<circle class="job-track" cx="50" cy="50" r="{radius}" />'
        f'<circle class="job-value" cx="50" cy="50" r="{radius}" '
        f'stroke-dasharray="{circ:.2f}" stroke-dashoffset="{offset:.2f}" />'
        f"</svg><span>{pct}%</span></div>"
        f'<div class="job-copy"><p class="job-kicker">{kicker}</p>'
        f'<p class="job-label">{safe_label}</p></div></div>'
    )


class _JobMeter:
    """Circular completion indicator. Updated from the job stages in this screen."""

    def __init__(self) -> None:
        self._slot = st.empty()

    def update(self, pct: int, label: str) -> None:
        self._slot.markdown(_ring_html(pct, label), unsafe_allow_html=True)

    def complete(self, label: str = "The workbook is ready for download.") -> None:
        self._slot.markdown(_ring_html(100, label, done=True), unsafe_allow_html=True)

    def clear(self) -> None:
        self._slot.empty()


def _watch_steps(meter: _JobMeter):
    """Map a stage counter onto the ring, leaving the last slice for completion."""

    def _on(done: int, total: int, label: str) -> None:
        fraction = 0.0 if total <= 0 else done / total
        meter.update(4 + int(96 * fraction), label)

    return _on


def _watch_portion(meter: _JobMeter, *, file_index: int, file_count: int):
    """Map one file's stages into its share of a multi-file job."""

    def _on(done: int, total: int, label: str) -> None:
        inner = 0.0 if total <= 0 else done / total
        overall = (file_index + inner) / max(file_count, 1)
        meter.update(4 + int(88 * overall), label)

    return _on


def _app_password() -> str:
    # Local runs on this Mac skip the team password. Railway still uses APP_PASSWORD.
    if os.environ.get("RBWS_LOCAL", "").strip() == "1":
        return ""
    env = os.environ.get("APP_PASSWORD", "").strip()
    if env:
        return env
    try:
        return str(st.secrets.get("APP_PASSWORD", "") or "").strip()
    except Exception:
        return ""


def _safe_pdf_name(raw: str) -> str:
    name = Path(raw or "").name.strip()
    if not name or name in {".", ".."} or any(sep in name for sep in ("/", "\\")):
        return "report.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name


def _clear_workbook() -> None:
    for key in (
        "excel_bytes",
        "excel_name",
        "excel_token",
        "findings_n",
        "warnings",
        "workflow_done",
    ):
        st.session_state.pop(key, None)


ensure_download_route()


def _require_login() -> bool:
    password_expected = _app_password()
    if not password_expected:
        return True
    if st.session_state.get("authenticated"):
        return True
    _render_mast()
    st.markdown(
        '<p class="panel-label">Access control</p><p class="work-title">Authorized sign-in</p>',
        unsafe_allow_html=True,
    )
    password = st.text_input("Team password", type="password")
    if st.button("Sign in", type="primary"):
        if not password.strip():
            st.error("Enter the team password.")
        elif password == password_expected:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("The password is not recognized.")
    _render_footer()
    return False


def _run_fire_water(uploaded, *, max_pages: int, skip_images: bool) -> None:
    safe_name = _safe_pdf_name(uploaded.name)
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)
        pdf_path = temp_dir / safe_name
        pdf_path.write_bytes(uploaded.getvalue())
        out_path = temp_dir / f"{pdf_path.stem}_RBWS.xlsx"
        work_dir = temp_dir / "work"
        meter = _JobMeter()
        try:
            meter.update(4, f"Document accepted · {safe_name}")
            result = OcrPipeline().run(
                pdf_path=pdf_path,
                output_xlsx=out_path,
                work_dir=work_dir,
                max_pages=int(max_pages),
                skip_images=skip_images,
                on_progress=_watch_steps(meter),
            )
            _token = publish_workbook(out_path, out_path.name)
            st.session_state.excel_token = _token
            st.session_state.excel_name = out_path.name
            st.session_state.findings_n = len(result.findings)
            st.session_state.warnings = list(result.warnings)
            st.session_state.workflow_done = WORKFLOW_FIRE_WATER
            meter.complete(f"Procedure A complete · {len(result.findings)} rows · {out_path.name}")
        except Exception:
            meter.clear()
            raise


def _save_line_uploads(line_files, line_zips, line_dir: Path) -> tuple[int, list[str]]:
    """Persist line PDFs (and optional zip packs) into line_dir. Returns (count, notes)."""
    notes: list[str] = []
    saved = 0
    seen_names: set[str] = set()

    def _write_pdf(name: str, data: bytes) -> None:
        nonlocal saved
        safe = _safe_pdf_name(name)
        if "summary" in safe.lower():
            notes.append(f"ignored summary-named file in line pack: {safe}")
            return
        if len(data) < 500:
            notes.append(f"ignored tiny/empty upload: {safe}")
            return
        # Avoid silent overwrite when browsers collide on names
        out_name = safe
        if out_name.lower() in seen_names:
            stem = Path(safe).stem
            out_name = f"{stem}__{saved + 1}.pdf"
        seen_names.add(out_name.lower())
        (line_dir / out_name).write_bytes(data)
        saved += 1

    for item in line_files or []:
        _write_pdf(item.name, item.getvalue())

    for zitem in line_zips or []:
        raw = zitem.getvalue()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if not info.filename.lower().endswith(".pdf"):
                        continue
                    base = Path(info.filename).name
                    if base.startswith(".") or base.startswith("__MACOSX"):
                        continue
                    _write_pdf(base, zf.read(info))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Could not read zip `{zitem.name}`: {exc}") from exc
        notes.append(f"unpacked zip: {zitem.name}")

    return saved, notes


def _run_edop(summary_file, line_files, line_zips, *, skip_images: bool) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)
        summary_name = _safe_pdf_name(summary_file.name)
        summary_path = temp_dir / summary_name
        summary_bytes = summary_file.getvalue()
        if len(summary_bytes) < 1000:
            raise ValueError("The summary document is empty. Submit the summary report again.")
        summary_path.write_bytes(summary_bytes)

        line_dir = temp_dir / "lines"
        line_dir.mkdir(parents=True, exist_ok=True)
        saved, save_notes = _save_line_uploads(line_files, line_zips, line_dir)

        kind = detect_pdf_kind(summary_path)
        # Fire Water PDF dropped into the piping summary slot with no line pack
        # → route to Workflow A instead of failing on checklist parse.
        if saved == 0 and kind == "fire_water":
            st.info(
                "The submitted document is a fire water inspection report. "
                "It will be processed under Procedure A."
            )
            _run_fire_water(summary_file, max_pages=0, skip_images=skip_images)
            return

        if saved == 0:
            raise ValueError(
                "No line documents were received. For process piping, submit the "
                "line documents or one archive. For a single fire water report, "
                "use Procedure A or automatic classification."
            )

        out_path = temp_dir / "ProcessPiping_RBWS_REVIEW.xlsx"
        work_dir = temp_dir / "work"
        meter = _JobMeter()
        try:
            meter.update(4, f"Summary accepted · {saved} line document(s)")
            result = build_edop_result(
                summary_pdf=summary_path,
                line_pdf_dir=line_dir,
                work_dir=work_dir,
                on_progress=_watch_portion(meter, file_index=0, file_count=1),
            )
            if skip_images:
                for finding in result.findings:
                    finding.photos_image_path = None
                    finding.photo_image_paths = []
                    finding.iso_image_path = None
            meter.update(94, "Writing the workbook")
            RbwsExcelWriter().write(
                result,
                out_path,
                embed_images=not skip_images,
            )
            _token = publish_workbook(out_path, out_path.name)
            st.session_state.excel_token = _token
            st.session_state.excel_name = out_path.name
            st.session_state.findings_n = len(result.findings)
            st.session_state.warnings = (
                [f"summary_detected_as={kind}"] + save_notes + list(result.warnings)
            )
            st.session_state.workflow_done = WORKFLOW_EDOP
            st.session_state.edop_saved_lines = saved
            meter.complete(
                f"Procedure B complete · {len(result.findings)} rows · {out_path.name}"
            )
        except Exception:
            meter.clear()
            raise


def _run_vessel(uploaded, *, skip_images: bool) -> None:
    """Pressure vessel workflow. One or more PDFs. Does not call Workflow A or B."""
    files = uploaded if isinstance(uploaded, list) else [uploaded]
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)
        meter = _JobMeter()
        try:
            meter.update(4, f"{len(files)} report(s) accepted")
            if len(files) == 1:
                uploaded_one = files[0]
                safe_name = _safe_pdf_name(uploaded_one.name)
                pdf_path = temp_dir / safe_name
                pdf_path.write_bytes(uploaded_one.getvalue())
                out_path = temp_dir / f"{pdf_path.stem}_RBWS.xlsx"
                result = build_vessel_result(
                    pdf_path,
                    temp_dir / "work",
                    on_progress=_watch_portion(meter, file_index=0, file_count=1),
                )
                if skip_images:
                    for finding in result.findings:
                        finding.photos_image_path = None
                        finding.photo_image_paths = []
                        finding.iso_image_path = None
                meter.update(94, "Writing the workbook")
                RbwsExcelWriter().write(
                    result,
                    out_path,
                    embed_images=not skip_images,
                    include_pof_note=False,
                )
                findings_n = len(result.findings)
                warnings = list(result.warnings)
                excel_name = out_path.name
            else:
                from openpyxl import Workbook

                wb = Workbook()
                writer = RbwsExcelWriter()
                findings_n = 0
                warnings = []
                file_count = len(files)
                for index, item in enumerate(files):
                    safe_name = _safe_pdf_name(item.name)
                    pdf_path = temp_dir / f"{index + 1:02d}_{safe_name}"
                    pdf_path.write_bytes(item.getvalue())
                    result = build_vessel_result(
                        pdf_path,
                        temp_dir / f"work_{index + 1}",
                        on_progress=_watch_portion(
                            meter, file_index=index, file_count=file_count
                        ),
                    )
                    if skip_images:
                        for finding in result.findings:
                            finding.photos_image_path = None
                            finding.photo_image_paths = []
                            finding.iso_image_path = None
                    writer.write(
                        result,
                        temp_dir / "vessel_RBWS.xlsx",
                        embed_images=not skip_images,
                        include_pof_note=False,
                        workbook=wb,
                        save=False,
                    )
                    findings_n += len(result.findings)
                    warnings.extend(f"{safe_name}: {note}" for note in result.warnings)
                if len(wb.worksheets) > 1:
                    wb.remove(wb.worksheets[0])
                meter.update(94, "Writing the workbook")
                out_path = temp_dir / "Pressure_vessel_RBWS.xlsx"
                wb.save(out_path)
                excel_name = out_path.name
            _token = publish_workbook(out_path, excel_name)
            st.session_state.excel_token = _token
            st.session_state.excel_name = excel_name
            st.session_state.findings_n = findings_n
            st.session_state.warnings = warnings
            st.session_state.workflow_done = WORKFLOW_VESSEL
            meter.complete(f"Pressure vessel workflow complete · {findings_n} rows · {excel_name}")
        except Exception:
            meter.clear()
            raise


def _run_auto(
    primary_file,
    line_files,
    line_zips,
    *,
    skip_images: bool,
    max_pages: int,
) -> None:
    """Route Fire Water vs Process Piping from the uploaded PDF content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)
        primary_path = temp_dir / _safe_pdf_name(primary_file.name)
        primary_path.write_bytes(primary_file.getvalue())
        kind = detect_pdf_kind(primary_path)

    has_lines = bool(line_files) or bool(line_zips)
    if kind == "fire_water" or (kind == "unknown" and not has_lines):
        if kind == "unknown":
            st.info("The report family could not be confirmed. It will be processed as a single fire water report.")
        else:
            st.info("Fire water inspection report identified. Processing under Procedure A.")
        _run_fire_water(primary_file, max_pages=max_pages, skip_images=skip_images)
        return

    if kind in {"process_piping_summary", "process_piping_unknown", "process_piping_line"} or has_lines:
        if not has_lines and kind == "process_piping_line":
            raise ValueError(
                "This document is a single process-piping line report. "
                "Submit the summary checklist together with the line documents, or one archive."
            )
        if not has_lines:
            raise ValueError(
                "Process piping was identified. Submit the line documents, or one archive, "
                "together with the summary, then generate the workbook again."
            )
        st.info("Process piping pack identified. Processing under Procedure B.")
        _run_edop(primary_file, line_files, line_zips, skip_images=skip_images)
        return

    raise ValueError(
        "The report family could not be identified. Use Procedure A for one fire water report, "
        "or Procedure B for a process piping summary with its line documents."
    )


st.set_page_config(page_title="RBWS FVI Workbook Generator", layout="wide")

if not _require_login():
    st.stop()

if not _app_password():
    st.warning(
        "Access control is not configured. Set APP_PASSWORD before this address is shared."
    )

_render_chrome()

def _show_run_error(exc: Exception) -> None:
    _clear_workbook()
    st.error(f"The workbook could not be produced. {exc}")
    # Keep stack traces for unexpected bugs only.
    if not isinstance(exc, ValueError):
        st.exception(exc)


with st.sidebar:
    st.markdown('<p class="side-label">Procedure</p>', unsafe_allow_html=True)
    workflow = st.radio(
        "Select procedure",
        options=[WORKFLOW_AUTO, WORKFLOW_FIRE_WATER, WORKFLOW_EDOP, WORKFLOW_VESSEL],
        index=0,
    )
    skip_images = st.checkbox("Omit photographs and isometric sketches", value=False)
    max_pages = 0
    if workflow in {WORKFLOW_FIRE_WATER, WORKFLOW_AUTO}:
        max_pages = st.number_input(
            "Page limit (0 processes the full document)",
            min_value=0,
            value=0,
            step=1,
        )
    st.markdown(
        '<p class="side-note">Use <strong>automatic classification</strong> when the report family is uncertain. '
        "Source files are retained only for the duration of the job. Extraction is rules-based.</p>",
        unsafe_allow_html=True,
    )

if st.session_state.get("active_workflow") != workflow:
    st.session_state.active_workflow = workflow
    _clear_workbook()

if workflow == WORKFLOW_AUTO:
    st.markdown(
        '<p class="panel-label">Automatic classification</p>'
        '<p class="work-title">Identify the report family from the submitted documents</p>',
        unsafe_allow_html=True,
    )
    st.info(
        "**Fire water.** Submit one complete inspection report.\n\n"
        "**Process piping.** Submit the summary checklist and the line documents, or one archive. "
        "The matching procedure is selected from the documents received."
    )
    primary = st.file_uploader(
        "Inspection report or piping summary",
        type=["pdf"],
        key="auto_primary",
    )
    line_files = st.file_uploader(
        "Line documents — process piping only",
        type=["pdf"],
        accept_multiple_files=True,
        key="auto_lines",
    )
    line_zips = st.file_uploader(
        "Line archive — process piping only",
        type=["zip"],
        accept_multiple_files=True,
        key="auto_zips",
    )
    ready = primary is not None
    if st.button("Generate workbook", type="primary", disabled=not ready, key="run_auto"):
        try:
            _run_auto(
                primary,
                line_files,
                line_zips,
                skip_images=skip_images,
                max_pages=int(max_pages),
            )
        except Exception as exc:
            _show_run_error(exc)
    elif not ready:
        st.markdown(
            '<p class="status-line"><em>Awaiting documents</em> · Submit a report to begin.</p>',
            unsafe_allow_html=True,
        )

elif workflow == WORKFLOW_FIRE_WATER:
    st.markdown(
        '<p class="panel-label">Procedure A</p>'
        '<p class="work-title">Fire water full visual inspection</p>',
        unsafe_allow_html=True,
    )
    st.info(
        "For a Jimcel **fire water** inspection report. "
        "Submit **one** complete PDF."
    )
    uploaded = st.file_uploader(
        "Fire water inspection report",
        type=["pdf"],
        key="fw_pdf",
        help="One complete fire water full visual inspection report",
    )
    if uploaded is not None:
        st.markdown(
            f'<p class="status-line"><em>Document received</em> · {_safe_pdf_name(uploaded.name)}</p>',
            unsafe_allow_html=True,
        )
    ready = uploaded is not None
    if st.button("Generate workbook", type="primary", disabled=not ready, key="run_a"):
        try:
            _run_fire_water(uploaded, max_pages=int(max_pages), skip_images=skip_images)
        except Exception as exc:
            _show_run_error(exc)
    elif not ready:
        st.markdown(
            '<p class="status-line"><em>Awaiting documents</em> · Submit the fire water report, then generate the workbook.</p>',
            unsafe_allow_html=True,
        )

elif workflow == WORKFLOW_EDOP:
    st.markdown(
        '<p class="panel-label">Procedure B</p>'
        '<p class="work-title">Process piping full visual inspection</p>',
        unsafe_allow_html=True,
    )
    st.info(
        "For **process piping** packs.\n\n"
        "1. Submit the **summary checklist**. Line descriptions are taken from the From–To text.\n"
        "2. Submit **each line document**, or one **archive** of those documents.\n\n"
        "A fire water report submitted here without line documents is identified and processed under Procedure A."
    )
    summary_file = st.file_uploader(
        "1 · Summary checklist",
        type=["pdf"],
        key="edop_summary",
        help="Process piping summary or observations checklist",
    )
    line_files = st.file_uploader(
        "2 · Line documents",
        type=["pdf"],
        accept_multiple_files=True,
        key="edop_lines",
        help="One PDF for each line on the piping list",
    )
    line_zips = st.file_uploader(
        "Or one archive of line documents",
        type=["zip"],
        accept_multiple_files=True,
        key="edop_zips",
        help="Optional archive containing the line documents",
    )
    if summary_file is not None:
        size = getattr(summary_file, "size", None) or 0
        st.markdown(
            f'<p class="status-line"><em>Summary received</em> · {_safe_pdf_name(summary_file.name)}'
            f'{f" · {size:,} bytes" if size else ""}</p>',
            unsafe_allow_html=True,
        )
    n_lines = len(line_files or [])
    n_zips = len(line_zips or [])
    if n_lines or n_zips:
        st.markdown(
            f'<p class="status-line"><em>Line documents received</em> · {n_lines} PDF(s)'
            f'{f" · {n_zips} zip(s)" if n_zips else ""}</p>',
            unsafe_allow_html=True,
        )
    # Summary alone is enough to attempt Fire Water auto-route; otherwise need lines.
    ready = summary_file is not None
    if st.button("Generate workbook", type="primary", disabled=not ready, key="run_b"):
        try:
            _run_edop(
                summary_file,
                line_files,
                line_zips,
                skip_images=skip_images,
            )
        except Exception as exc:
            _show_run_error(exc)
    elif not ready:
        st.markdown(
            '<p class="status-line"><em>Awaiting documents</em> · Submit the summary checklist. '
            "Add the line documents for process piping.</p>",
            unsafe_allow_html=True,
        )

elif workflow == WORKFLOW_VESSEL:
    st.markdown(
        '<p class="panel-label">Pressure vessel workflow</p>'
        '<p class="work-title">Pressure vessel and tank full visual inspection</p>',
        unsafe_allow_html=True,
    )
    st.info(
        "For a **pressure vessel or tank** inspection report from any facility. "
        "Submit one report, or several. Each report is read from its summary of findings "
        "and the equipment photographs. One equipment item is one row. "
        "Several reports are issued as one workbook, with one sheet per report. "
        "Probability of failure and recommendations remain blank for engineering review."
    )
    vessel_pdfs = st.file_uploader(
        "Pressure vessel inspection reports",
        type=["pdf"],
        key="vessel_pdf",
        accept_multiple_files=True,
        help="One or more full visual inspection reports for pressure vessels and tanks",
    )
    if vessel_pdfs:
        loaded = ", ".join(_safe_pdf_name(item.name) for item in vessel_pdfs)
        st.markdown(
            f'<p class="status-line"><em>Documents received</em> · {len(vessel_pdfs)} file(s) · {loaded}</p>',
            unsafe_allow_html=True,
        )
    vessel_ready = bool(vessel_pdfs)
    if st.button("Generate workbook", type="primary", disabled=not vessel_ready, key="run_vessel"):
        try:
            _run_vessel(vessel_pdfs, skip_images=skip_images)
        except Exception as exc:
            _show_run_error(exc)
    elif not vessel_ready:
        st.markdown(
            '<p class="status-line"><em>Awaiting documents</em> · Submit a pressure vessel report, then generate the workbook.</p>',
            unsafe_allow_html=True,
        )

if st.session_state.get("excel_token"):
    findings_n = st.session_state.get("findings_n", 0)
    done = st.session_state.get("workflow_done", "Job")
    excel_name = st.session_state.get("excel_name", "workbook.xlsx")
    st.markdown(
        f'<p class="status-line"><em>Workbook ready</em> · {done} · {findings_n} rows · '
        f"{excel_name}</p>",
        unsafe_allow_html=True,
    )
    warnings = st.session_state.get("warnings") or []
    if warnings:
        with st.expander("Processing notes"):
            for warning in warnings:
                st.write(f"- {warning}")
    st.link_button(
        "Download workbook",
        f"/rbws-download/{st.session_state.excel_token}",
    )

_render_footer()
