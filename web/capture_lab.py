"""Render full-window desktop screenshots of the interactive Quant Blue workbench
tabs via headless Microsoft Edge and optionally mirror copies to an artifact folder.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

EDGE_PATH = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
OUTPUT_DIR = ROOT / "output"
INDEX_HTML = WEB_DIR / "index.html"


def file_url(path: Path, query: str = "") -> str:
    resolved = path.resolve().as_posix()
    encoded = quote(resolved, safe="/:")
    return f"file:///{encoded}{query}"


def capture_tab(tab: str, out_png: Path, width: int = 1440, height: int = 960, scale: int = 2) -> None:
    if not EDGE_PATH.exists():
        raise FileNotFoundError(f"Microsoft Edge not found at {EDGE_PATH}")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    url = file_url(INDEX_HTML, f"?tab={tab}")
    with tempfile.TemporaryDirectory(prefix=f"edge_lab_{tab}_") as tmp_profile:
        cmd = [
            str(EDGE_PATH),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={tmp_profile}",
            f"--force-device-scale-factor={scale}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=2500",
            f"--screenshot={out_png}",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not out_png.exists() or out_png.stat().st_size == 0:
            raise RuntimeError(
                f"Edge screenshot failed for tab={tab} (rc={proc.returncode})\n"
                f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
            )


def dump_dom_check(tab: str) -> str:
    url = file_url(INDEX_HTML, f"?tab={tab}")
    with tempfile.TemporaryDirectory(prefix=f"edge_dom_{tab}_") as tmp_profile:
        cmd = [
            str(EDGE_PATH),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={tmp_profile}",
            "--virtual-time-budget=2000",
            "--dump-dom",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"Edge --dump-dom failed for tab={tab}: {proc.stderr}")
        return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture desktop screenshots of the Blended 5-20m VWAP workbench.")
    parser.add_argument("--artifact-dir", type=Path, default=None, help="Optional folder to copy final screenshots into.")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--include-sheet", action="store_true", help="Also capture Tab 3 (Editorial Blue Sheet).")
    args = parser.parse_args()

    targets = [
        ("lab", OUTPUT_DIR / "workbench_tab1_lab.png", "workbench_tab1_lab.png"),
        ("attribution", OUTPUT_DIR / "workbench_tab2_attribution.png", "workbench_tab2_attribution.png"),
        ("research", OUTPUT_DIR / "workbench_tab3_research.png", "workbench_tab3_research.png"),
    ]
    if args.include_sheet:
        targets.append(("sheet", OUTPUT_DIR / "workbench_tab4_sheet.png", "workbench_tab4_sheet.png"))

    for tab, out_path, artifact_name in targets:
        capture_tab(tab, out_path, width=args.width, height=args.height, scale=args.scale)
        print(f"Captured [{tab}] -> {out_path} ({out_path.stat().st_size:,} bytes)")
        if args.artifact_dir is not None:
            args.artifact_dir.mkdir(parents=True, exist_ok=True)
            dest = args.artifact_dir / artifact_name
            shutil.copy2(out_path, dest)
            print(f"Copied artifact -> {dest}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
