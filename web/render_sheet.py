"""Render Page-1 (blue_sheet.html) and Page-2 (research_brief.html) to high-DPI PNG & 1-page US Letter PDF via headless Edge."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
OUTPUT_DIR = ROOT_DIR / "output"

SHEETS = {
    "page1": {
        "label": "Page 1 — Map of Blended 5-20m VWAP",
        "html": WEB_DIR / "blue_sheet.html",
        "png": OUTPUT_DIR / "blended_vwap_blue_sheet.png",
        "pdf": OUTPUT_DIR / "blended_vwap_blue_sheet.pdf",
    },
    "page2": {
        "label": "Page 2 — OU-Spectral Research Brief",
        "html": WEB_DIR / "research_brief.html",
        "png": OUTPUT_DIR / "ou_spectral_research_brief.png",
        "pdf": OUTPUT_DIR / "ou_spectral_research_brief.pdf",
    },
}

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
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)


def render_sheet(
    edge_bin: Path,
    html_path: Path,
    png_path: Path,
    pdf_path: Path,
    label: str,
    copy_preview_to: Path | None = None,
) -> None:
    if not html_path.exists():
        raise FileNotFoundError(f"Missing source HTML ({label}): {html_path}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    file_url = html_path.resolve().as_uri()

    # 1. Render 2x high-DPI PNG (2200 x 2848 px)
    png_res = run_edge(
        edge_bin,
        [
            f"--force-device-scale-factor={SCALE_FACTOR}",
            f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
            "--hide-scrollbars",
            "--virtual-time-budget=4000",
            f"--screenshot={png_path.resolve()}",
            file_url,
        ],
    )
    if png_res.returncode != 0 or not png_path.exists():
        raise RuntimeError(
            f"PNG render failed for {label} (rc={png_res.returncode}): {png_res.stderr.strip()}"
        )

    # 2. Render single-page US Letter PDF
    pdf_res = run_edge(
        edge_bin,
        [
            "--no-pdf-header-footer",
            "--virtual-time-budget=4000",
            f"--print-to-pdf={pdf_path.resolve()}",
            file_url,
        ],
    )
    if pdf_res.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(
            f"PDF render failed for {label} (rc={pdf_res.returncode}): {pdf_res.stderr.strip()}"
        )

    with Image.open(png_path) as im:
        width, height = im.size
    pdf_pages = count_pdf_pages(pdf_path)

    print(f"[{label}] PNG: {png_path} ({width}x{height}px, {png_path.stat().st_size:,} bytes)")
    print(f"[{label}] PDF: {pdf_path} ({pdf_pages} page(s), {pdf_path.stat().st_size:,} bytes)")

    if (width, height) != (VIEWPORT_W * SCALE_FACTOR, VIEWPORT_H * SCALE_FACTOR):
        raise RuntimeError(
            f"Expected {VIEWPORT_W * SCALE_FACTOR}x{VIEWPORT_H * SCALE_FACTOR}px PNG for {label}, got {width}x{height}px"
        )
    if pdf_pages != 1:
        raise RuntimeError(f"Expected 1-page PDF for {label}, got {pdf_pages} pages!")

    if copy_preview_to is not None:
        copy_preview_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(png_path, copy_preview_to)
        print(f"[{label}] Copied preview PNG to: {copy_preview_to}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render IEEE Quant Blue sheets (Page 1 & Page 2).")
    parser.add_argument(
        "--only",
        choices=["page1", "page2", "both"],
        default="both",
        help="Which sheet(s) to render (default: both).",
    )
    parser.add_argument("--preview-p1", type=Path, default=None, help="Optional preview PNG path for Page 1.")
    parser.add_argument("--preview-p2", type=Path, default=None, help="Optional preview PNG path for Page 2.")
    args = parser.parse_args()

    edge_bin = find_edge()
    targets = ["page1", "page2"] if args.only == "both" else [args.only]

    for key in targets:
        spec = SHEETS[key]
        if key == "page2" and not spec["html"].exists() and args.only == "both":
            print(f"Skipping {spec['label']} (not found yet: {spec['html']})")
            continue
        preview = args.preview_p1 if key == "page1" else args.preview_p2
        render_sheet(
            edge_bin=edge_bin,
            html_path=spec["html"],
            png_path=spec["png"],
            pdf_path=spec["pdf"],
            label=spec["label"],
            copy_preview_to=preview,
        )


if __name__ == "__main__":
    main()
