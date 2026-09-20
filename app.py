import tempfile
from pathlib import Path

import streamlit as st

from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline

st.set_page_config(page_title="RBWS FVI Excel Generator", layout="wide")

st.markdown(
    """
    <style>
    .main {
        background: linear-gradient(180deg, #f5f7fb 0%, #eef4ff 100%);
    }
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    .hero {
        background: linear-gradient(135deg, #0f172a 0%, #1d4ed8 100%);
        border-radius: 18px;
        padding: 1.5rem 1.5rem;
        color: white;
        box-shadow: 0 8px 24px rgba(0,0,0,0.08);
    }
    .info-card {
        background: rgba(255,255,255,0.78);
        border: 1px solid rgba(15,23,42,0.08);
        border-radius: 16px;
        padding: 1rem 1.1rem;
        box-shadow: 0 4px 16px rgba(15,23,42,0.04);
    }
    .upload-box {
        border: 2px dashed #93c5fd;
        border-radius: 16px;
        background: rgba(239,246,255,0.7);
        padding: 1rem;
    }
    .small-muted {
        color: #475569;
        font-size: 0.9rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
        <h2 style="margin:0; font-weight:700;">RBWS FVI Excel Generator</h2>
        <p style="margin:0.5rem 0 0 0; opacity:0.9;">Upload an FVI PDF and download the RBWS Excel workbook locally.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.info(
    "Active rule set: Visual Inspection Observation table is the primary source; summary/detail pages verify and supplement only when necessary; photo references stay in one consolidated range; engineering judgment fields remain blank."
)

col_left, col_right = st.columns([1.5, 1])

with col_left:
    st.markdown(
        """
        <div class="info-card">
            <strong>Processing rules</strong>
            <ul>
                <li>Sections marked N/A produce zero rows.</li>
                <li>Visual Inspection Observation table is the master record source.</li>
                <li>Duplicate asset references are merged into one record.</li>
                <li>Photo captions are only verification/supplementary input.</li>
                <li>Engineering judgment fields remain blank in the output Excel.</li>
            </ul>
        </div>
        """,
        unsafe_allow_html=True,
    )

with col_right:
    st.markdown(
        """
        <div class="info-card">
            <strong>Blank Excel fields</strong>
            <div class="small-muted">Line Pressure, POF, Consq, Driver, Risk, IRU, ISO, Quest Recommendations, Remarks</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("<div style='height: 1rem'></div>", unsafe_allow_html=True)

with st.container():
    st.markdown('<div class="upload-box">', unsafe_allow_html=True)
    uploaded = st.file_uploader(
        "Choose a PDF file",
        type=["pdf"],
        help="Upload a single FVI PDF report to convert into an RBWS Excel file.",
        label_visibility="visible",
    )
    st.markdown("</div>", unsafe_allow_html=True)

if uploaded is not None:
    st.success(f"Selected: {uploaded.name}")

    with st.sidebar:
        st.header("Processing settings")
        max_pages = st.number_input(
            "Maximum pages to process",
            min_value=0,
            value=0,
            step=1,
            help="Set to 0 to process all pages.",
        )
        skip_images = st.checkbox(
            "Skip cropped photo / ISO images",
            value=False,
            help="Disable embedded photos and ISO sketches in the Excel output.",
        )
        st.caption("Runs locally without external LLM APIs.")

    if st.button("Generate Excel", type="primary", use_container_width=True):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            pdf_path = temp_dir / uploaded.name
            pdf_path.write_bytes(uploaded.getvalue())

            out_path = temp_dir / f"{pdf_path.stem}_rbws.xlsx"
            work_dir = temp_dir / "work"

            st.info(
                f"Processing {uploaded.name}. The app is applying the universal extraction logic: master rows from visual inspection tables, deduplication, and blank engineering judgment fields."
            )
            try:
                pipeline = OcrPipeline()
                result = pipeline.run(
                    pdf_path=pdf_path,
                    output_xlsx=out_path,
                    work_dir=work_dir,
                    max_pages=int(max_pages),
                    skip_images=skip_images,
                )

                st.success(
                    f"Extraction complete. {len(result.findings)} findings generated using the master record rule set."
                )

                if result.warnings:
                    with st.expander("Warnings / validation notes", expanded=False):
                        for warning in result.warnings:
                            st.write(f"- {warning}")

                with open(out_path, "rb") as fh:
                    excel_bytes = fh.read()

                st.download_button(
                    label="Download RBWS Excel",
                    data=excel_bytes,
                    file_name=f"{pdf_path.stem}_rbws.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except Exception as exc:
                st.error(f"Processing failed: {exc}")
                st.exception(exc)
else:
    st.info("Upload a PDF to begin the RBWS extraction process.")

st.markdown("---")
st.caption("Built for local PDF-to-Excel conversion with no external LLM dependency.")
