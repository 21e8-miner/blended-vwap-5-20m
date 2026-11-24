"""Render web/blue_sheet.html to high-DPI PNG and single-page US Letter PDF via headless Edge."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

ROOT_DIR = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT_DIR / "web" / "blue_sheet.html"
OUTPUT_DIR = ROOT_DIR / "output"
PNG_PATH = OUTPUT_DIR / "blended_vwap_blue_sheet.png"
PDF_PATH = OUTPUT_DIR / "blended_vwap_blue_sheet.pdf"

VIEWPORT_W = 1100
VIEWPORT_H = 1424
SCALE_FACTOR = 2


def find_edge() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Microsoft Edge binary not found in standard Program Files paths."
    )


def count_pdf_pages(pdf_path: Path) -> int:
    """Count pages in a Chromium-generated PDF without external PDF libraries."""
    raw = pdf_path.read_bytes()
    # Chromium emits a /Pages dictionary with /Count N as well as individual /Type /Page objects.
    counts = [int(m) for m in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", raw, re.DOTALL)]
    if counts:
        return max(counts)
    pages = re.findall(rb"/Type\s*/Page\b(?!s)", raw)
    return len(pages)


def run_edge(edge_bin: Path, args: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    tmp_profile = tempfile.mkdtemp(prefix="edge_blue_sheet_")
    cmd = [
        str(edge_bin),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-extensions",
        "--disable-background-networking",
        "--allow-file-access-from-files",
        f"--user-data-dir={tmp_profile}",
        *args,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)


def render_all(copy_preview_to: Path | None = None) -> None:
    if not HTML_PATH.exists():
        raise FileNotFoundError(f"Missing source HTML: {HTML_PATH}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    edge_bin = find_edge()
    file_url = HTML_PATH.resolve().as_uri()

    # 1. Render 2x high-DPI PNG (2200 x 2848 px)
    png_res = run_edge(
        edge_bin,
        [
            f"--force-device-scale-factor={SCALE_FACTOR}",
            f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
            "--hide-scrollbars",
            "--virtual-time-budget=4000",
            f"--screenshot={PNG_PATH.resolve()}",
            file_url,
        ],
    )
    if png_res.returncode != 0 or not PNG_PATH.exists():
        raise RuntimeError(
            f"PNG render failed (rc={png_res.returncode}): {png_res.stderr.strip()}"
        )

    # 2. Render single-page US Letter PDF
    pdf_res = run_edge(
        edge_bin,
        [
            "--no-pdf-header-footer",
            "--virtual-time-budget=4000",
            f"--print-to-pdf={PDF_PATH.resolve()}",
            file_url,
        ],
    )
    if pdf_res.returncode != 0 or not PDF_PATH.exists():
        raise RuntimeError(
            f"PDF render failed (rc={pdf_res.returncode}): {pdf_res.stderr.strip()}"
        )

    # Verify PNG dimensions and PDF single-page fit
    with Image.open(PNG_PATH) as im:
        width, height = im.size
    pdf_pages = count_pdf_pages(PDF_PATH)

    print(f"Rendered PNG: {PNG_PATH} ({width}x{height}px, {PNG_PATH.stat().st_size:,} bytes)")
    print(f"Rendered PDF: {PDF_PATH} ({pdf_pages} page(s), {PDF_PATH.stat().st_size:,} bytes)")

    if pdf_pages != 1:
        raise RuntimeError(f"Expected 1-page PDF, got {pdf_pages} pages!")

    if copy_preview_to is not None:
        copy_preview_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PNG_PATH, copy_preview_to)
        print(f"Copied preview PNG to: {copy_preview_to}")


if __name__ == "__main__":
    preview_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    render_all(copy_preview_to=preview_arg)
