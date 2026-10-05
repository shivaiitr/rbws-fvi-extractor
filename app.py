import os
import tempfile
from pathlib import Path

import streamlit as st

from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline

st.set_page_config(page_title="RBWS FVI Excel Generator", layout="wide")

def _app_password() -> str:
    env = os.environ.get("APP_PASSWORD", "").strip()
    if env:
        return env
    try:
        return str(st.secrets.get("APP_PASSWORD", "") or "").strip()
    except Exception:
        return ""


def _require_login() -> bool:
    password_expected = _app_password()
    if not password_expected:
        return True
    if st.session_state.get("authenticated"):
        return True
    st.title("RBWS FVI Excel Generator")
    st.caption("Enter the shared team password to continue.")
    password = st.text_input("Password", type="password")
    if st.button("Unlock", type="primary"):
        if password == password_expected:
            st.session_state.authenticated = True
            st.rerun()
        st.error("Wrong password.")
    return False


if not _require_login():
    st.stop()

st.markdown(
    """
    <style>
    .hero {
        background: linear-gradient(135deg, #0f172a 0%, #1d4ed8 100%);
        border-radius: 16px;
        padding: 1.25rem 1.5rem;
        color: white;
        margin-bottom: 1rem;
    }
    </style>
    <div class="hero">
        <h2 style="margin:0;">RBWS FVI Excel Generator</h2>
        <p style="margin:0.4rem 0 0 0; opacity:0.9;">
            Upload a Fire Water FVI PDF. The tool builds the RBWS Excel locally — no LLM API.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    **How it works:** Visual Inspection tables → one row per item → crop photos into Excel.  
    Left blank: Line Pressure, POF, Consq, Driver, Risk, IRU, Quest Recommendations, Remarks.
    """
)

with st.sidebar:
    st.header("Settings")
    max_pages = st.number_input(
        "Max pages (0 = all)",
        min_value=0,
        value=0,
        step=1,
    )
    skip_images = st.checkbox("Skip photo embeds (faster)", value=False)
    st.caption("Runs on this server. PDFs stay in a temporary folder for the job, then are deleted.")

uploaded = st.file_uploader("FVI PDF", type=["pdf"])

if uploaded is not None:
    st.success(f"Selected: {uploaded.name}")

if st.button("Generate Excel", type="primary", use_container_width=True, disabled=uploaded is None):
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)
        pdf_path = temp_dir / uploaded.name
        pdf_path.write_bytes(uploaded.getvalue())
        out_path = temp_dir / f"{pdf_path.stem}_RBWS.xlsx"
        work_dir = temp_dir / "work"
        with st.spinner(f"Processing {uploaded.name}… this can take a few minutes."):
            try:
                result = OcrPipeline().run(
                    pdf_path=pdf_path,
                    output_xlsx=out_path,
                    work_dir=work_dir,
                    max_pages=int(max_pages),
                    skip_images=skip_images,
                )
                st.session_state.excel_bytes = out_path.read_bytes()
                st.session_state.excel_name = f"{pdf_path.stem}_RBWS.xlsx"
                st.session_state.findings_n = len(result.findings)
                st.session_state.warnings = list(result.warnings)
            except Exception as exc:
                st.session_state.excel_bytes = None
                st.error(f"Processing failed: {exc}")
                st.exception(exc)

if st.session_state.get("excel_bytes"):
    st.success(f"Done. {st.session_state.get('findings_n', 0)} finding rows.")
    warnings = st.session_state.get("warnings") or []
    if warnings:
        with st.expander("Notes"):
            for warning in warnings:
                st.write(f"- {warning}")
    st.download_button(
        label="Download RBWS Excel",
        data=st.session_state.excel_bytes,
        file_name=st.session_state.get("excel_name", "RBWS.xlsx"),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
elif uploaded is None:
    st.info("Upload a PDF, then click Generate Excel.")
