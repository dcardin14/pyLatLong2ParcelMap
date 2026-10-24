#!/usr/bin/env python3
"""Locate a Texas land parcel from coordinates and render an aerial PNG map."""

import argparse
import re
import sys
import webbrowser
import zipfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Works without a Linux desktop/X11 session.
import matplotlib.pyplot as plt
import geopandas as gpd
import requests
from shapely.geometry import Point

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"
DOWNLOAD_BASE = (
    "https://tnris-data-warehouse.s3.amazonaws.com/"
    "LCD/collection/stratmap-2026-land-parcels/items/shp/"
)
COUNTY_QUERY = "https://maps.dot.state.tx.us/arcgis/rest/services/Boundaries/MapServer/1/query"
FIELDS = ("OWNER_NAME", "PROP_ID", "GEO_ID", "LEGAL_AREA", "LGL_AREA_U", "LEGAL_DESC", "SITUS_ADDR")


def parse_coord(value):
    """Parse signed decimal degrees or e.g. 30d18m22sN, 95°27'14\"W."""
    s = value.strip().upper().replace("°", "D").replace("’", "'").replace("”", '"')
    try:
        return float(s)
    except ValueError:
        pass
    match = re.fullmatch(
        r"([+-]?)\s*(\d+(?:\.\d+)?)\s*D"
        r"(?:\s*(\d+(?:\.\d+)?)\s*(?:M|'))?"
        r"(?:\s*(\d+(?:\.\d+)?)\s*(?:S|\"|''))?"
        r"\s*([NSEW]?)", s
    )
    if not match:
        raise ValueError(f"Cannot parse coordinate: {value!r}")
    sign, deg, minutes, seconds, hemi = match.groups()
    if sign == "-" and hemi in ("N", "E") or sign == "+" and hemi in ("S", "W"):
        raise ValueError("Conflicting sign and hemisphere")
    if float(minutes or 0) >= 60 or float(seconds or 0) >= 60:
        raise ValueError("Minutes and seconds must be less than 60")
    result = float(deg) + float(minutes or 0) / 60 + float(seconds or 0) / 3600
    return -result if sign == "-" or hemi in ("S", "W") else result


def find_county(lat, lon, county_file=None):
    if county_file:
        counties = gpd.read_file(county_file).to_crs(4326)
        matches = counties[counties.geometry.intersects(Point(lon, lat))]
        if matches.empty:
            raise RuntimeError("Coordinates are outside the supplied county boundaries")
        row = matches.iloc[0]
        name = row.get("CNTY_NM", row.get("NAME", "Unknown"))
        code = row.get("FIPS_NBR", row.get("CNTY_FIPS", row.get("FIPS")))
    else:
        # Query only the point against TxDOT's statewide county layer, no local GeoJSON required.
        response = requests.get(COUNTY_QUERY, params={
            "f": "json", "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": "CNTY_NM,FIPS_NBR", "returnGeometry": "false",
        }, timeout=45)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"County service error: {data['error']}")
        features = data.get("features", [])
        if not features:
            raise RuntimeError("Point is not inside a Texas county")
        row = features[0]["attributes"]
        name, code = row["CNTY_NM"], row["FIPS_NBR"]
    if code is None:
        raise RuntimeError("County data lacks a FIPS code")
    # The parcel archive name uses the three-digit county code, not Texas's 48 prefix.
    digits = re.sub(r"\D", "", str(code))
    return str(name), digits[-3:].zfill(3)


def download_parcels(fips, cache_dir):
    folder = cache_dir / fips
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "source.zip"
    if archive.is_file() and zipfile.is_zipfile(archive):
        print(f"Using cached ZIP: {archive}")
        return archive
    url = DOWNLOAD_BASE + f"stratmap-2026-land-parcels_{fips}_shp.zip"
    print(f"Downloading: {url}")
    tmp = archive.with_suffix(".partial")
    try:
        with requests.get(url, stream=True, timeout=(20, 180)) as response:
            response.raise_for_status()
            with tmp.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        if not zipfile.is_zipfile(tmp):
            raise RuntimeError("Downloaded content is not a valid ZIP file")
        tmp.replace(archive)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return archive


def locate_shapefile(archive, folder):
    existing = sorted(folder.rglob("*.shp"))
    if existing:
        return existing[0]
    print("Extracting parcel archive...")
    with zipfile.ZipFile(archive) as zf:
        root = folder.resolve()
        for member in zf.infolist():
            target = (folder / member.filename).resolve()
            if not target.is_relative_to(root):
                raise RuntimeError("Unsafe path found in parcel ZIP")
            if member.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, target.open("wb") as dest:
                import shutil
                shutil.copyfileobj(source, dest)
    files = sorted(folder.rglob("*.shp"))
    if not files:
        raise RuntimeError("No .shp file found inside county archive")
    return files[0]


def find_parcel(parcels, point):
    hits = list(parcels.sindex.query(point, predicate="intersects"))
    print(f"Candidate parcels: {len(hits)}")
    if not hits:
        raise RuntimeError("No parcel intersects these coordinates")
    # All coordinate operations occur in the shapefile's native CRS.
    for _, row in parcels.iloc[hits].iterrows():
        if row.geometry is not None and row.geometry.intersects(point):
            return row
    raise RuntimeError("No parcel contains these coordinates")


def field(row, name):
    value = row.get(name, "")
    return "" if value is None else str(value)


def create_map(parcels, selected, point, lat, lon, county, output_dir, aerial=True):
    import contextily as ctx
    polygon = selected.geometry
    minx, miny, maxx, maxy = polygon.bounds
    span = max(maxx - minx, maxy - miny)
    padding = max(span * 1.5, 20 if parcels.crs.is_projected else 0.0002)
    nearby = parcels.cx[minx-padding:maxx+padding, miny-padding:maxy+padding].copy()
    selected_layer = gpd.GeoDataFrame([selected], geometry="geometry", crs=parcels.crs).to_crs(3857)
    nearby = nearby.to_crs(3857)
    location_layer = gpd.GeoSeries([point], crs=parcels.crs).to_crs(3857)
    x, y = location_layer.iloc[0].x, location_layer.iloc[0].y
    xmin, ymin, xmax, ymax = selected_layer.total_bounds
    xpad, ypad = max((xmax-xmin)*0.65, 100), max((ymax-ymin)*0.65, 100)
    bounds = (xmin-xpad, xmin + (xmax-xmin) + xpad, ymin-ypad, ymax+ypad)

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.set_xlim(bounds[0], bounds[1]); ax.set_ylim(bounds[2], bounds[3])
    if aerial:
        try:
            ctx.add_basemap(ax, source=ctx.providers.Esri.WorldImagery, crs="EPSG:3857")
        except Exception as exc:
            print(f"Warning: aerial tiles unavailable ({exc}); rendering parcel-only map.", file=sys.stderr)
    nearby.boundary.plot(ax=ax, color="lightgray", linewidth=0.6, zorder=2)
    selected_layer.plot(ax=ax, facecolor="#fff5a5", alpha=0.5, edgecolor="red", linewidth=2, zorder=3)
    ax.scatter([x], [y], color="blue", s=65, zorder=5)
    owner = field(selected, "OWNER_NAME")
    centroid = selected_layer.geometry.iloc[0].representative_point()
    label = f"{owner}\nParcel: {field(selected, 'PROP_ID')}\nGeo ID: {field(selected, 'GEO_ID')}\nArea: {field(selected, 'LEGAL_AREA')} {field(selected, 'LGL_AREA_U')}"
    ax.annotate(label, (centroid.x, centroid.y), ha="center", fontsize=8,
                bbox={"facecolor": "white", "alpha": 0.85}, zorder=6)
    ax.annotate(f"{lat:.6f}, {lon:.6f}", (x, y), xytext=(8, 8), textcoords="offset points", color="blue")
    for _, neighbor in nearby.iterrows():
        if field(neighbor, "PROP_ID") == field(selected, "PROP_ID"):
            continue
        name = field(neighbor, "OWNER_NAME")
        if not name or name.lower() == "nan":
            continue
        p = neighbor.geometry.representative_point()
        if bounds[0] <= p.x <= bounds[1] and bounds[2] <= p.y <= bounds[3]:
            ax.annotate(name, (p.x, p.y), ha="center", fontsize=6,
                        bbox={"facecolor":"white", "alpha":0.7, "edgecolor":"none", "pad":1.5})
    ax.set_xlim(bounds[0], bounds[1]); ax.set_ylim(bounds[2], bounds[3])
    ax.set_title(f"{owner}\nParcel {field(selected, 'PROP_ID')} | Geo ID {field(selected, 'GEO_ID')}")
    ax.set_axis_off()
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{county}_{field(selected, 'PROP_ID')}_{owner}")[:140].strip("_.") or "parcel"
    destination = output_dir / f"{basename}.png"
    fig.savefig(destination, dpi=250, bbox_inches="tight")
    plt.close(fig)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="Map a Texas parcel from latitude,longitude")
    parser.add_argument("coordinates", help='Example: "30.31,-95.46" or "30d18m36sN,95d27m36sW"')
    parser.add_argument("--county-file", type=Path, help="Optional local Texas counties GeoJSON/shapefile")
    parser.add_argument("--cache-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--no-aerial", action="store_true", help="Do not download basemap tiles")
    parser.add_argument("--open", action="store_true", help="Open the saved image with your desktop's default application")
    args = parser.parse_args(argv)
    try:
        lat_str, lon_str = args.coordinates.split(",", 1)
        lat, lon = parse_coord(lat_str), parse_coord(lon_str)
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("Latitude or longitude out of range")
        print(f"Coordinates: {lat:.6f}, {lon:.6f}")
        name, fips = find_county(lat, lon, args.county_file)
        print(f"County: {name} (FIPS {fips})")
        archive = download_parcels(fips, args.cache_dir)
        shp = locate_shapefile(archive, args.cache_dir / fips)
        print(f"Reading: {shp}")
        parcels = gpd.read_file(shp)
        if parcels.crs is None:
            raise RuntimeError("Parcel shapefile has no CRS; cannot safely locate points")
        print(f"Parcel records: {len(parcels):,} | CRS: {parcels.crs}")
        native_point = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(parcels.crs).iloc[0]
        selected = find_parcel(parcels, native_point)
        print("\nPARCEL FOUND")
        for key in FIELDS:
            print(f"{key:12}: {field(selected, key)}")
        output = create_map(parcels, selected, native_point, lat, lon, name, args.output_dir, not args.no_aerial)
        print(f"\nMap created: {output}")
        if args.open:
            webbrowser.open(output.resolve().as_uri())
        return 0
    except (ValueError, RuntimeError, requests.RequestException, OSError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
