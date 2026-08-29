#!/usr/bin/env python3
"""Plate-solve MicroObservatory exoplanet FITS frames with Astrometry.net.

The MicroObservatory FITS headers in this dataset contain pointing RA/Dec and
image scale, but not a solved celestial WCS. This script submits source lists
to Astrometry.net, then converts the header target RA/Dec into image pixels.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import sep
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.utils.exceptions import AstropyWarning
from astropy.wcs import WCS
from astroquery.astrometry_net import AstrometryNet


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "_plate_solve_results"
SCIENCE_GLOB = "*.FITS"
DARK_DIR = "darks"


@dataclass
class SolveResult:
    target_folder: str
    fits_file: str
    object_name: str
    target_ra_deg: float
    target_dec_deg: float
    target_x_px_0_indexed: float | None
    target_y_px_0_indexed: float | None
    target_x_px_fits_1_indexed: float | None
    target_y_px_fits_1_indexed: float | None
    image_width: int
    image_height: int
    detected_source_count: int
    submitted_source_count: int
    solved: bool
    status: str
    submission_id: int | None = None
    wcs_file: str | None = None


def science_fits_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.glob(SCIENCE_GLOB) if DARK_DIR not in path.parts)


def median_dark(folder: Path, shape: tuple[int, int]) -> np.ndarray | None:
    darks = []
    for path in sorted((folder / DARK_DIR).glob(SCIENCE_GLOB)):
        data = fits.getdata(path).astype(np.float32)
        if data.shape == shape:
            darks.append(data)
    if not darks:
        return None
    return np.median(np.stack(darks), axis=0).astype(np.float32)


def calibrated_data(path: Path, dark: np.ndarray | None) -> np.ndarray:
    data = fits.getdata(path).astype(np.float32)
    if dark is not None:
        data = data - dark
    return np.ascontiguousarray(np.nan_to_num(data, nan=float(np.nanmedian(data))))


def extract_sources(data: np.ndarray, threshold_sigma: float) -> list[dict[str, float]]:
    background = sep.Background(data)
    objects = sep.extract(
        data - background.back(),
        threshold_sigma,
        err=background.globalrms,
        minarea=6,
    )
    height, width = data.shape
    sources: list[dict[str, float]] = []
    for obj in objects:
        x = float(obj["x"])
        y = float(obj["y"])
        flux = float(obj["flux"])
        peak = float(obj["peak"])
        a = float(obj["a"])
        b = float(obj["b"])
        if not (5 < x < width - 5 and 5 < y < height - 5):
            continue
        if flux <= 0 or a < 0.85 or b < 0.65:
            continue
        if peak >= 0.95 * 4095:
            continue
        sources.append({"x": x, "y": y, "flux": flux, "peak": peak, "a": a, "b": b})
    sources.sort(key=lambda source: source["flux"], reverse=True)
    return sources


def choose_reference_frame(folder: Path, threshold_sigma: float) -> tuple[Path, np.ndarray, list[dict[str, float]], np.ndarray | None]:
    files = science_fits_files(folder)
    if not files:
        raise RuntimeError("No science FITS files found.")

    shape = fits.getdata(files[0]).shape
    dark = median_dark(folder, shape)
    best: tuple[int, Path, np.ndarray, list[dict[str, float]]] | None = None
    for path in files:
        data = calibrated_data(path, dark)
        sources = extract_sources(data, threshold_sigma)
        score = len(sources)
        if best is None or score > best[0]:
            best = (score, path, data, sources)
    if best is None:
        raise RuntimeError("Could not choose a reference frame.")
    _, path, data, sources = best
    return path, data, sources, dark


def solve_one(
    astrometry: AstrometryNet,
    path: Path,
    data: np.ndarray,
    sources: list[dict[str, float]],
    output_dir: Path,
    max_sources: int,
    solve_timeout: int,
) -> SolveResult:
    header = fits.getheader(path)
    height, width = data.shape
    object_name = str(header.get("OBJECT") or path.parent.name)
    target_ra = float(header["RA"])
    target_dec = float(header["DEC"])

    selected_sources = sources[:max_sources]
    if len(selected_sources) < 6:
        return SolveResult(
            target_folder=path.parent.name,
            fits_file=str(path),
            object_name=object_name,
            target_ra_deg=target_ra,
            target_dec_deg=target_dec,
            target_x_px_0_indexed=None,
            target_y_px_0_indexed=None,
            target_x_px_fits_1_indexed=None,
            target_y_px_fits_1_indexed=None,
            image_width=width,
            image_height=height,
            detected_source_count=len(sources),
            submitted_source_count=len(selected_sources),
            solved=False,
            status="too_few_sources_for_astrometry_submission",
        )

    x = [source["x"] for source in selected_sources]
    y = [source["y"] for source in selected_sources]
    wcs_header, submission_id = astrometry.solve_from_source_list(
        x,
        y,
        image_width=width,
        image_height=height,
        center_ra=target_ra,
        center_dec=target_dec,
        radius=1.0,
        scale_type="ul",
        scale_units="arcsecperpix",
        scale_lower=3.0,
        scale_upper=7.0,
        publicly_visible="n",
        allow_commercial_use="n",
        allow_modifications="n",
        solve_timeout=solve_timeout,
        verbose=True,
        return_submission_id=True,
    )
    if not wcs_header:
        return SolveResult(
            target_folder=path.parent.name,
            fits_file=str(path),
            object_name=object_name,
            target_ra_deg=target_ra,
            target_dec_deg=target_dec,
            target_x_px_0_indexed=None,
            target_y_px_0_indexed=None,
            target_x_px_fits_1_indexed=None,
            target_y_px_fits_1_indexed=None,
            image_width=width,
            image_height=height,
            detected_source_count=len(sources),
            submitted_source_count=len(selected_sources),
            solved=False,
            status="astrometry_timeout_or_no_solution",
            submission_id=submission_id,
        )

    wcs = WCS(wcs_header)
    target = SkyCoord(target_ra, target_dec, unit="deg")
    x_px, y_px = wcs.world_to_pixel(target)

    safe_stem = f"{path.parent.name}_{path.stem}".replace("/", "_").replace(" ", "_")
    wcs_path = output_dir / f"{safe_stem}.wcs.fits"
    fits.PrimaryHDU(header=wcs_header).writeto(wcs_path, overwrite=True)

    return SolveResult(
        target_folder=path.parent.name,
        fits_file=str(path),
        object_name=object_name,
        target_ra_deg=target_ra,
        target_dec_deg=target_dec,
        target_x_px_0_indexed=float(x_px),
        target_y_px_0_indexed=float(y_px),
        target_x_px_fits_1_indexed=float(x_px + 1),
        target_y_px_fits_1_indexed=float(y_px + 1),
        image_width=width,
        image_height=height,
        detected_source_count=len(sources),
        submitted_source_count=len(selected_sources),
        solved=True,
        status="solved",
        submission_id=submission_id,
        wcs_file=str(wcs_path),
    )


def target_folders(root: Path, names: Iterable[str] | None = None) -> list[Path]:
    allowed = set(names or [])
    folders = [
        path
        for path in sorted(root.iterdir())
        if path.is_dir() and path.name != DARK_DIR and not path.name.startswith("_") and path.name != "__pycache__"
    ]
    if allowed:
        folders = [path for path in folders if path.name in allowed]
    return folders


def write_outputs(results: list[SolveResult], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "target_pixel_coordinates.json"
    csv_path = output_dir / "target_pixel_coordinates.csv"
    json_path.write_text(json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()) if results else list(SolveResult.__dataclass_fields__))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--target", action="append", help="Limit to one target folder name. May be repeated.")
    parser.add_argument("--all-frames", action="store_true", help="Submit every science FITS frame instead of only the best reference frame per folder.")
    parser.add_argument("--max-sources", type=int, default=80)
    parser.add_argument("--threshold-sigma", type=float, default=3.0)
    parser.add_argument("--solve-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="Only report the selected files/source counts; do not submit to Astrometry.net.")
    args = parser.parse_args()

    api_key = os.getenv("ASTROMETRY_NET_API_KEY") or os.getenv("ASTROMETRY_API_KEY") or os.getenv("AN_API_KEY")
    if not api_key and not args.dry_run:
        raise SystemExit("Set ASTROMETRY_NET_API_KEY before running, or use --dry-run.")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    folders = target_folders(args.root.expanduser().resolve(), args.target)
    if not folders:
        raise SystemExit("No target folders found.")

    astrometry = AstrometryNet()
    if api_key:
        astrometry.api_key = api_key

    results: list[SolveResult] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AstropyWarning)
        for folder in folders:
            ref_path, ref_data, ref_sources, dark = choose_reference_frame(folder, args.threshold_sigma)
            paths = science_fits_files(folder) if args.all_frames else [ref_path]
            print(f"{folder.name}: selected {ref_path.name} with {len(ref_sources)} detected sources")
            if args.dry_run:
                continue
            for path in paths:
                data = ref_data if path == ref_path else calibrated_data(path, dark)
                sources = ref_sources if path == ref_path else extract_sources(data, args.threshold_sigma)
                print(f"  solving {path.name}: {len(sources)} sources")
                try:
                    result = solve_one(
                        astrometry=astrometry,
                        path=path,
                        data=data,
                        sources=sources,
                        output_dir=output_dir,
                        max_sources=args.max_sources,
                        solve_timeout=args.solve_timeout,
                    )
                except Exception as error:
                    header = fits.getheader(path)
                    height, width = data.shape
                    result = SolveResult(
                        target_folder=folder.name,
                        fits_file=str(path),
                        object_name=str(header.get("OBJECT") or folder.name),
                        target_ra_deg=float(header.get("RA", np.nan)),
                        target_dec_deg=float(header.get("DEC", np.nan)),
                        target_x_px_0_indexed=None,
                        target_y_px_0_indexed=None,
                        target_x_px_fits_1_indexed=None,
                        target_y_px_fits_1_indexed=None,
                        image_width=width,
                        image_height=height,
                        detected_source_count=len(sources),
                        submitted_source_count=min(len(sources), args.max_sources),
                        solved=False,
                        status=f"error: {error}",
                    )
                results.append(result)
                print(f"    {result.status}")

    if results:
        write_outputs(results, output_dir)
        print(f"Wrote {output_dir / 'target_pixel_coordinates.csv'}")
        print(f"Wrote {output_dir / 'target_pixel_coordinates.json'}")
    elif args.dry_run:
        print("Dry run complete; no Astrometry.net submissions were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
