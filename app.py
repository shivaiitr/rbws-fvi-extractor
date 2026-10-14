"""Quest Global RBWS FVI Extractor — Streamlit UI for two report workflows."""

from __future__ import annotations

import base64
import io
import os
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from rbws_extractor.excel.writer import RbwsExcelWriter
from rbws_extractor.ocr.edop_piping import build_edop_result
from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline

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
    grid-template-columns: 1fr 1fr;
    gap: 0;
    margin: 0 0 1.25rem;
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

WORKFLOW_FIRE_WATER = "Workflow A · Fire Water FVI (single PDF)"
WORKFLOW_EDOP = "Workflow B · Process Piping (summary + line PDFs)"


def _logo_html() -> str:
    if not LOGO_PATH.exists():
        return ""
    encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return (
        f'<img class="qg-logo" src="data:image/svg+xml;base64,{encoded}" '
        'alt="Quest Global" width="210">'
    )


def _render_chrome() -> None:
    st.markdown(QG_CSS, unsafe_allow_html=True)
    st.markdown(
        f"""
        <header class="mast">
            <div>{_logo_html()}</div>
            <div>
                <p class="mast-kicker">Quest Global Engineering Services Limited</p>
                <h1>RBWS FVI Extractor</h1>
                <p class="mast-sub">Two inspection workflows → gold-layout RBWS Excel. Local OCR/rules only — no LLM calls.</p>
            </div>
            <div class="mast-meta">
                <strong>Workflow A</strong>
                Fire Water · 1 PDF
                <strong>Workflow B</strong>
                Process Piping · summary + lines
            </div>
        </header>
        <section class="workflow-grid">
            <div class="workflow-card">
                <span class="idx">A</span>
                <strong>Fire Water FVI (e.g. UBIT-GP)</strong>
                <p>One Jimcel Fire Water inspection PDF. Upload that report only.</p>
                <ul>
                    <li>Observation tables → one row per item</li>
                    <li>Photos / ISO crops embedded</li>
                    <li>Judgment columns left blank</li>
                </ul>
            </div>
            <div class="workflow-card">
                <span class="idx">B</span>
                <strong>Process Piping (e.g. EDOP-QP)</strong>
                <p>Summary checklist PDF <em>plus</em> one PDF per line number.</p>
                <ul>
                    <li>Summary → Line Description (From–To)</li>
                    <li>Line PDF → anomaly text under images + photo crops</li>
                    <li>No images in a line pack → <code>no data</code></li>
                </ul>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_footer() -> None:
    st.markdown(
        '<p class="qg-footer">Quest Global Engineering Services Limited · <strong>Built by Shivendra</strong></p>',
        unsafe_allow_html=True,
    )


def _app_password() -> str:
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
    for key in ("excel_bytes", "excel_name", "findings_n", "warnings", "workflow_done"):
        st.session_state.pop(key, None)


def _require_login() -> bool:
    password_expected = _app_password()
    if not password_expected:
        return True
    if st.session_state.get("authenticated"):
        return True
    _render_chrome()
    st.markdown(
        '<p class="panel-label">Controlled access</p><p class="work-title">Team sign-in</p>',
        unsafe_allow_html=True,
    )
    password = st.text_input("Shared password", type="password")
    if st.button("Enter", type="primary"):
        if not password.strip():
            st.error("Enter the password.")
        elif password == password_expected:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("The password does not match.")
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
        with st.spinner(
            f"Workflow A · extracting {safe_name}. Large Fire Water reports can take several minutes."
        ):
            result = OcrPipeline().run(
                pdf_path=pdf_path,
                output_xlsx=out_path,
                work_dir=work_dir,
                max_pages=int(max_pages),
                skip_images=skip_images,
            )
            st.session_state.excel_bytes = out_path.read_bytes()
            st.session_state.excel_name = out_path.name
            st.session_state.findings_n = len(result.findings)
            st.session_state.warnings = list(result.warnings)
            st.session_state.workflow_done = WORKFLOW_FIRE_WATER


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
            raise ValueError("Summary PDF upload is empty. Re-upload the summary report.")
        summary_path.write_bytes(summary_bytes)

        line_dir = temp_dir / "lines"
        line_dir.mkdir(parents=True, exist_ok=True)
        saved, save_notes = _save_line_uploads(line_files, line_zips, line_dir)
        if saved == 0:
            raise ValueError(
                "No line-number PDFs were saved. Upload the per-line PDFs "
                "(or a .zip of them). Names like `8._AI-001-….pdf` work best."
            )

        out_path = temp_dir / "EDOP-QP_RBWS_REVIEW.xlsx"
        work_dir = temp_dir / "work"
        with st.spinner(
            f"Workflow B · summary + {saved} line PDF(s). This can take several minutes."
        ):
            result = build_edop_result(
                summary_pdf=summary_path,
                line_pdf_dir=line_dir,
                work_dir=work_dir,
            )
            if skip_images:
                for finding in result.findings:
                    finding.photos_image_path = None
                    finding.iso_image_path = None
            RbwsExcelWriter().write(
                result,
                out_path,
                embed_images=not skip_images,
            )
            st.session_state.excel_bytes = out_path.read_bytes()
            st.session_state.excel_name = out_path.name
            st.session_state.findings_n = len(result.findings)
            st.session_state.warnings = save_notes + list(result.warnings)
            st.session_state.workflow_done = WORKFLOW_EDOP
            st.session_state.edop_saved_lines = saved


st.set_page_config(page_title="RBWS FVI Extractor", layout="wide")

if not _require_login():
    st.stop()

if not _app_password():
    st.warning(
        "APP_PASSWORD is not set. Anyone who can open this URL can run reports. "
        "Set it in the environment or Streamlit secrets before sharing."
    )

_render_chrome()

with st.sidebar:
    st.markdown('<p class="side-label">Job controls</p>', unsafe_allow_html=True)
    workflow = st.radio(
        "Select workflow",
        options=[WORKFLOW_FIRE_WATER, WORKFLOW_EDOP],
        index=0,
    )
    skip_images = st.checkbox("Omit photograph / ISO embeds", value=False)
    max_pages = 0
    if workflow == WORKFLOW_FIRE_WATER:
        max_pages = st.number_input(
            "Page limit (0 = full document)",
            min_value=0,
            value=0,
            step=1,
        )
    st.markdown(
        '<p class="side-note">Uploads stay in a temporary folder for this job and are removed when processing finishes. Processing is local OCR/rules — no LLM API calls.</p>',
        unsafe_allow_html=True,
    )

if st.session_state.get("active_workflow") != workflow:
    st.session_state.active_workflow = workflow
    _clear_workbook()

if workflow == WORKFLOW_FIRE_WATER:
    st.markdown(
        '<p class="panel-label">Workflow A</p>'
        '<p class="work-title">Fire Water FVI — single report PDF</p>',
        unsafe_allow_html=True,
    )
    st.info(
        "Use this for Jimcel **Fire Water** reports (example: UBIT-GP). "
        "Upload **one** full inspection PDF. The app builds RBWS rows from observation tables and crops photos/ISO."
    )
    uploaded = st.file_uploader(
        "Fire Water inspection PDF",
        type=["pdf"],
        key="fw_pdf",
        help="Single FVI Fire Water PDF",
    )
    if uploaded is not None:
        st.markdown(
            f'<p class="status-line"><em>Loaded</em> · {_safe_pdf_name(uploaded.name)}</p>',
            unsafe_allow_html=True,
        )
    ready = uploaded is not None
    if st.button("Generate workbook", type="primary", disabled=not ready, key="run_a"):
        try:
            _run_fire_water(uploaded, max_pages=int(max_pages), skip_images=skip_images)
        except Exception as exc:
            _clear_workbook()
            st.error(f"Processing failed: {exc}")
            st.exception(exc)
    elif not ready:
        st.markdown(
            '<p class="status-line"><em>Waiting</em> · Upload the Fire Water PDF, then generate.</p>',
            unsafe_allow_html=True,
        )

else:
    st.markdown(
        '<p class="panel-label">Workflow B</p>'
        '<p class="work-title">Process Piping — summary + line-number PDFs</p>',
        unsafe_allow_html=True,
    )
    st.info(
        "Use this for **EDOP-QP Process Piping** (and similar packs).\n\n"
        "1. Upload the **summary / checklist** PDF only (From–To line descriptions).\n"
        "2. Upload **all line-number PDFs**, or one **.zip** of them "
        "(anomaly text under images + photo crops).\n\n"
        "Keep Workflow **B** selected. Do not put line PDFs in the summary slot."
    )
    summary_file = st.file_uploader(
        "1 · Summary / checklist PDF (required)",
        type=["pdf"],
        key="edop_summary",
        help="EDOP-QP FVI On Process Piping Summary Report — not a line PDF",
    )
    line_files = st.file_uploader(
        "2a · Line-number PDFs (multi-select)",
        type=["pdf"],
        accept_multiple_files=True,
        key="edop_lines",
        help="One PDF per JV Piping List S/N (e.g. 8._AI-001-….pdf)",
    )
    line_zips = st.file_uploader(
        "2b · Or zip of line-number PDFs",
        type=["zip"],
        accept_multiple_files=True,
        key="edop_zips",
        help="Optional: zip containing the line PDFs (more reliable than 100+ browser uploads)",
    )
    if summary_file is not None:
        size = getattr(summary_file, "size", None) or 0
        st.markdown(
            f'<p class="status-line"><em>Summary</em> · {_safe_pdf_name(summary_file.name)}'
            f'{f" · {size:,} bytes" if size else ""}</p>',
            unsafe_allow_html=True,
        )
    n_lines = len(line_files or [])
    n_zips = len(line_zips or [])
    if n_lines or n_zips:
        st.markdown(
            f'<p class="status-line"><em>Line packs</em> · {n_lines} PDF(s)'
            f'{f" · {n_zips} zip(s)" if n_zips else ""}</p>',
            unsafe_allow_html=True,
        )
    ready = summary_file is not None and (n_lines > 0 or n_zips > 0)
    if st.button("Generate workbook", type="primary", disabled=not ready, key="run_b"):
        try:
            _run_edop(
                summary_file,
                line_files,
                line_zips,
                skip_images=skip_images,
            )
        except Exception as exc:
            _clear_workbook()
            st.error(f"Processing failed: {exc}")
            st.exception(exc)
    elif not ready:
        st.markdown(
            '<p class="status-line"><em>Waiting</em> · Upload the summary PDF and the line-number PDFs (or a zip), then generate.</p>',
            unsafe_allow_html=True,
        )

if st.session_state.get("excel_bytes"):
    findings_n = st.session_state.get("findings_n", 0)
    done = st.session_state.get("workflow_done", "Job")
    st.markdown(
        f'<p class="status-line"><em>Complete</em> · {done} · {findings_n} rows · '
        f'{st.session_state.get("excel_name", "workbook.xlsx")}</p>',
        unsafe_allow_html=True,
    )
    warnings = st.session_state.get("warnings") or []
    if warnings:
        with st.expander("Extraction notes"):
            for warning in warnings:
                st.write(f"- {warning}")
    st.download_button(
        label="Download RBWS Excel",
        data=st.session_state.excel_bytes,
        file_name=st.session_state.get("excel_name", "workbook.xlsx"),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

_render_footer()
