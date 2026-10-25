#!/usr/bin/env python3
"""Create a Texas parcel ownership map from the GPS metadata in a photograph.

This wrapper leaves pyLatLong2ParcelMap.py unchanged. Requires ExifTool on PATH
and the existing mapping script/dependencies in the same project directory.

Usage:
    python pyPhoto2Parcel.py PXL_20261024_141511506.MP.jpg
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
MAPPER = PROJECT_DIR / "pyLatLong2ParcelMap.py"


def extract_coordinates(photo: Path) -> tuple[float, float]:
    """Read numeric GPS coordinates directly from the original photo."""
    if not shutil.which("exiftool"):
        raise RuntimeError(
            "ExifTool is not installed. On Debian run: "
            "sudo apt install libimage-exiftool-perl"
        )

    result = subprocess.run(
        ["exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude", str(photo)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ExifTool failed: {result.stderr.strip()}")

    try:
        metadata = json.loads(result.stdout)[0]
        lat = float(metadata["GPSLatitude"])
        lon = float(metadata["GPSLongitude"])
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "No usable GPS latitude/longitude found in this photo. "
            "Transfer the original image from your phone rather than a resized "
            "or messaging-app copy."
        ) from exc

    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise RuntimeError("GPS coordinates are not finite numbers.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RuntimeError(f"GPS coordinates are out of range: {lat}, {lon}")
    if lat == 0 and lon == 0:
        raise RuntimeError("GPS coordinates are 0,0, likely an empty GPS tag.")
    return lat, lon


def run_mapper(lat: float, lon: float) -> Path:
    """Run the existing mapping program and locate its reported output PNG."""
    if not MAPPER.is_file():
        raise RuntimeError(f"Cannot find existing mapping script: {MAPPER}")

    coordinate_argument = f"{lat:.12f},{lon:.12f}"
    print(f"\nRunning: {MAPPER.name} {coordinate_argument}\n", flush=True)

    # Run in the project directory so its existing relative paths (counties/,
    # cache/, output/) continue working without changing the original script.
    process = subprocess.Popen(
        [sys.executable, str(MAPPER), coordinate_argument],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )

    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output_lines.append(line.rstrip("\r\n"))

    exit_code = process.wait()
    if exit_code != 0:
        raise RuntimeError(
            f"Parcel mapping failed (exit code {exit_code}).\\n"
            + "\\n".join(output_lines[-40:])
        )

    # Existing create_map() prints 'Map created:' then the relative filename.
    output_path_text = None
    for index, line in enumerate(output_lines):
        if line.strip() == "Map created:" and index + 1 < len(output_lines):
            output_path_text = output_lines[index + 1].strip()

    if not output_path_text:
        raise RuntimeError("Mapping completed, but no output filename was reported.")

    output_path = Path(output_path_text)
    if not output_path.is_absolute():
        output_path = PROJECT_DIR / output_path
    if not output_path.is_file():
        raise RuntimeError(f"Mapping reported a PNG that does not exist: {output_path}")
    return output_path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract photo GPS coordinates and generate a Texas parcel map."
    )
    parser.add_argument("photo", type=Path, help="Path to the original GPS-tagged photograph")
    args = parser.parse_args()

    photo = args.photo.expanduser().resolve()
    if not photo.is_file():
        parser.error(f"Photo not found: {photo}")

    try:
        lat, lon = extract_coordinates(photo)
        print(f"Photo: {photo.name}")
        print(f"GPS latitude:  {lat:.12f}")
        print(f"GPS longitude: {lon:.12f}")
        map_path = run_mapper(lat, lon)
    except (RuntimeError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        return 130

    print("\nSUCCESS — parcel map saved to:")
    print(map_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
