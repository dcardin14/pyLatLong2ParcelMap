import os
import re
import sys
import zipfile
import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
import matplotlib.pyplot as plt
import subprocess
import pyogrio

COUNTY_FILE = "counties/txdot_counties.geojson"

CACHE_DIR = "cache"
OUTPUT_DIR = "output"

DOWNLOAD_BASE = (
    "https://tnris-data-warehouse.s3.amazonaws.com/"
    "LCD/collection/stratmap-2026-land-parcels/"
    "items/shp/"
)




def find_county(lat, lon):

    counties = gpd.read_file(COUNTY_FILE)

    point = Point(lon, lat)

    match = counties[counties.contains(point)]

    if match.empty:
        raise RuntimeError(
            "Point is not inside any Texas county."
        )

    row = match.iloc[0]

    return row["CNTY_NM"], str(row["CNTY_FIPS"])

def find_parcel(gdf, lat, lon):

    point = Point(lon, lat)

    print()
    print("Building spatial index...")

    hits = list(
        gdf.sindex.query(
            point,
            predicate="intersects"
        )
    )

    print(f"Candidate parcels: {len(hits)}")

    if hits:
        candidates = gdf.iloc[hits]

        for _, row in candidates.iterrows():
            if (
                row.geometry.contains(point)
                or row.geometry.touches(point)
            ):
                print("Exact parcel match found.")
                return row

    # No exact match: find nearest parcel.
    print("No exact parcel match. Checking nearby parcels...")

    point_m = gpd.GeoSeries(
        [point],
        crs="EPSG:4326"
    ).to_crs("EPSG:32615").iloc[0]

    parcels_m = gdf.to_crs("EPSG:32615")

    distances = parcels_m.geometry.distance(point_m)
    nearest_idx = distances.idxmin()
    nearest_distance = distances.loc[nearest_idx]

    print(f"Nearest parcel distance: {nearest_distance:.2f} meters")

    max_distance = 15.0

    if nearest_distance <= max_distance:
        print(
            "WARNING: Using nearest parcel, "
            "not an exact GPS intersection."
        )
        return gdf.loc[nearest_idx]

    raise RuntimeError(
        f"No parcel found within {max_distance} meters. "
        f"Nearest parcel is {nearest_distance:.2f} meters away."
    )


def download_parcel_zip(fips):

    county_dir = os.path.join(
        CACHE_DIR,
        fips
    )

    os.makedirs(
        county_dir,
        exist_ok=True
    )

    zip_path = os.path.join(
        county_dir,
        "source.zip"
    )

    zip_name = (
        f"stratmap-2026-land-parcels_{fips}_shp.zip"
    )

    url = DOWNLOAD_BASE + zip_name

    if os.path.exists(zip_path):

        print()
        print("ZIP already cached.")
        print(zip_path)

        return zip_path

    print()
    print("Downloading:")
    print(url)

    r = requests.get(
        url,
        stream=True,
        timeout=600
    )

    r.raise_for_status()

    with open(zip_path, "wb") as f:

        for chunk in r.iter_content(
            chunk_size=1024 * 1024
        ):
            if chunk:
                f.write(chunk)

    print()
    print("Download complete.")

    return zip_path


def extract_zip(zip_path, fips):

    target_dir = os.path.join(
        CACHE_DIR,
        fips
    )

    shp_files = glob.glob(
        os.path.join(target_dir, "*.shp")
    )

    if shp_files:

        print()
        print("Already extracted.")

        return target_dir

    print()
    print("Extracting...")

    with zipfile.ZipFile(zip_path) as z:
        z.extractall(target_dir)

    print("Extraction complete.")

    return target_dir

import glob
import os

def create_map(gdf, parcel, lat, lon, county_name):

    point = Point(lon, lat)

    minx, miny, maxx, maxy = parcel.geometry.bounds

    xpad = max((maxx - minx) * 2, 0.005)
    ypad = max((maxy - miny) * 2, 0.005)

    nearby = gdf.cx[
        minx - xpad:maxx + xpad,
        miny - ypad:maxy + ypad
    ]

    parcel_gdf = gpd.GeoDataFrame(
        [parcel],
        geometry="geometry",
        crs="EPSG:4326"
    )

    point_gdf = gpd.GeoDataFrame(
        geometry=[point],
        crs="EPSG:4326"
    )

    nearby = nearby.to_crs(epsg=3857)
    parcel_gdf = parcel_gdf.to_crs(epsg=3857)
    point_gdf = point_gdf.to_crs(epsg=3857)

    fig, ax = plt.subplots(
        figsize=(10, 10)
    )

    nearby.boundary.plot(
        ax=ax,
        color="lightgray",
        linewidth=0.5
    )

    parcel_gdf.plot(
        ax=ax,
        facecolor="#fff5a5",
        edgecolor="red",
        linewidth=2
    )

    point_gdf.plot(
        ax=ax,
        color="blue",
        markersize=75
    )

    centroid = parcel_gdf.geometry.iloc[0].centroid
    px, py = point_gdf.geometry.iloc[0].coords[0]

    ax.annotate(
        (
            f"{parcel['OWNER_NAME']}\n"
            f"Parcel ID: {parcel['PROP_ID']}\n"
            f"Geo ID: {parcel['GEO_ID']}\n"
            f"Legal Area: {parcel['LEGAL_AREA']} {parcel['LGL_AREA_U']}"
        ),
        (centroid.x, centroid.y),
        ha="center",
        fontsize=8,
        bbox=dict(
            facecolor="white",
            alpha=0.8
        )
    )

    ax.annotate(
        f"{lat:.6f}, {lon:.6f}",
        (px, py),
        xytext=(8, 8),
        textcoords="offset points",
        color="blue"
    )

    pminx, pminy, pmaxx, pmaxy = parcel_gdf.total_bounds

    pxpad = max((pmaxx - pminx) * 0.5, 100)
    pypad = max((pmaxy - pminy) * 0.5, 100)

    ax.set_xlim(pminx - pxpad, pmaxx + pxpad)
    ax.set_ylim(pminy - pypad, pmaxy + pypad)

    # Label only neighbors inside the view to avoid clutter
    for _, row in nearby.iterrows():

        if row["PROP_ID"] == parcel["PROP_ID"]:
            continue

        owner = row["OWNER_NAME"]

        if not isinstance(owner, str) or not owner.strip():
            continue

        pt = row.geometry.representative_point()

        if not (
            pminx - pxpad <= pt.x <= pmaxx + pxpad
            and pminy - pypad <= pt.y <= pmaxy + pypad
        ):
            continue

        ax.annotate(
            owner,
            (pt.x, pt.y),
            ha="center",
            fontsize=6,
            color="black",
            bbox=dict(
                facecolor="white",
                alpha=0.7,
                edgecolor="none",
                pad=1.5
            )
        )

    ax.set_title(
        (
            f"{parcel['OWNER_NAME']}\n"
            f"Parcel {parcel['PROP_ID']}  |  Geo ID {parcel['GEO_ID']}"
        )
    )

    ax.set_axis_off()

    plt.tight_layout()

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )

    safe_name = re.sub(
        r'[<>:"/\\|?*]+',
        "",
        f"{county_name.upper()}_{parcel['PROP_ID']}_{parcel['OWNER_NAME']}"
    ).strip()

    output_file = os.path.join(
        OUTPUT_DIR,
        f"{safe_name}.png"
    )

    plt.savefig(
        output_file,
        dpi=150,
        bbox_inches="tight"
    )

    plt.close()

    print()
    print("Map created:")
    print(output_file)

    # os.startfile(output_file)  Only  make the map if it's on my local machine.
    if not os.environ.get("RAILWAY_ENVIRONMENT"):
        subprocess.Popen(["xdg-open", output_file])


def parse_coord(text):
    """Convert decimal degrees or D/M/S (e.g. 27d56m43s, -99d32m22s) to decimal degrees."""

    s = text.strip().upper().replace("°", "D").replace("’", "'").replace("”", '"')

    try:
        return float(s)
    except ValueError:
        pass

    m = re.fullmatch(
        r"([+-]?)\s*(\d+(?:\.\d+)?)\s*D"
        r"(?:\s*(\d+(?:\.\d+)?)\s*(?:M|'))?"
        r"(?:\s*(\d+(?:\.\d+)?)\s*(?:S|\"|''))?"
        r"\s*([NSEW]?)",
        s
    )

    if not m:
        raise ValueError(f"Cannot parse coordinate: {text!r}")

    sign, deg, minutes, seconds, hemi = m.groups()

    value = (
        float(deg)
        + float(minutes or 0) / 60
        + float(seconds or 0) / 3600
    )

    if sign == "-" or hemi in ("S", "W"):
        value = -value

    return value


def main():

    if len(sys.argv) < 2:
        sys.exit("Usage: python pyLatLong2ParcelMap.py LAT,LON")

    try:
        lat_text, lon_text = " ".join(sys.argv[1:]).split(",")
        lat = parse_coord(lat_text)
        lon = parse_coord(lon_text)
    except ValueError as e:
        sys.exit(f"{e}\nUsage: python pyLatLong2ParcelMap.py LAT,LON")

    print()
    print(f"Decimal degrees: {lat:.6f}, {lon:.6f}")

    county_name, county_fips = find_county(
        lat,
        lon
    )

    print()
    print("County:", county_name)
    print("FIPS:", county_fips)

    zip_path = download_parcel_zip(
        county_fips
    )

    extract_dir = extract_zip(
        zip_path,
        county_fips
    )

    print()
    print("Files extracted to:")
    print(extract_dir)

    print()
    print("Looking for shapefiles...")

    shp_files = glob.glob(
        os.path.join(
            extract_dir,
            "**",
            "*.shp"
        ),
        recursive=True
    )

    print()

    for shp in shp_files:
        print(shp)

    if not shp_files:
        raise RuntimeError(
            "No shapefiles found."
        )

   # ----------------------------------------
    from shapely.geometry import box
    from pyproj import CRS, Transformer
    from shapely.ops import transform

    search_radius = 0.01

    gps_bbox = box(
        lon - search_radius,
        lat - search_radius,
        lon + search_radius,
        lat + search_radius
    )

    nearby_frames = []

    for shp in sorted(shp_files):
        print()
        print("Searching shapefile:", shp, flush=True)

        source_crs = pyogrio.read_info(shp)["crs"]

        if source_crs is None:
            raise RuntimeError(
                f"Parcel shapefile has no defined CRS: {shp}"
            )

        if CRS.from_user_input(source_crs) != CRS.from_epsg(4326):
            source_transformer = Transformer.from_crs(
                "EPSG:4326",
                source_crs,
                always_xy=True
            )
            search_bbox = transform(
                source_transformer.transform,
                gps_bbox
            ).bounds
        else:
            search_bbox = gps_bbox.bounds

        nearby = gpd.read_file(shp, bbox=search_bbox)

        if not nearby.empty:
            nearby = nearby.to_crs(epsg=4326)
            nearby_frames.append(nearby)

        print(f"Nearby records: {len(nearby):,}", flush=True)

    if not nearby_frames:
        raise RuntimeError(
            "No parcel records found near the GPS coordinates."
        )

    gdf = gpd.GeoDataFrame(
        pd.concat(nearby_frames, ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326"
    )

    shapefile_crs = "EPSG:4326"
# -----------------------------------------

    print()
    print(f"Records: {len(gdf):,}")

    print("Shapefile CRS:", shapefile_crs)
    print("Loaded CRS:", gdf.crs)
    print("GPS point:", lon, lat)
    print("Loaded bounds:", gdf.total_bounds)
    print("Valid geometries:", gdf.geometry.is_valid.sum())
    print("Empty geometries:", gdf.geometry.is_empty.sum())
    point = Point(lon, lat)
    print("Direct intersection count:", gdf.geometry.intersects(point).sum())
    print("Spatial index bbox hits:", len(gdf.sindex.query(point)))

    point = gpd.GeoSeries(
        [Point(lon, lat)],
        crs="EPSG:4326"
    ).to_crs("EPSG:32615")

    parcels_m = gdf.to_crs("EPSG:32615")

    distances = parcels_m.geometry.distance(point.iloc[0])

    nearest_idx = distances.idxmin()

    print("Nearest parcel distance (meters):", distances.loc[nearest_idx])
    print("Nearest parcel owner:", gdf.loc[nearest_idx, "OWNER_NAME"])

    parcel = find_parcel(
        gdf,
        lat,
        lon
    )

    print()
    print("=" * 60)
    print("PARCEL FOUND")
    print("=" * 60)

    print(
        f"OWNER_NAME : "
        f"{parcel['OWNER_NAME']}"
    )

    print(
        f"PROP_ID    : "
        f"{parcel['PROP_ID']}"
    )

    print(
        f"GEO_ID     : "
        f"{parcel['GEO_ID']}"
    )

    print(
        f"LEGAL_AREA : "
        f"{parcel['LEGAL_AREA']} {parcel['LGL_AREA_U']}"
    )

    print(
        f"LEGAL_DESC : "
        f"{parcel['LEGAL_DESC']}"
    )

    print(
        f"SITUS_ADDR : "
        f"{parcel['SITUS_ADDR']}"
    )

    # Load neighboring parcels around the entire identified property.
    minx, miny, maxx, maxy = parcel.geometry.bounds

    xpad = max((maxx - minx) * 2, 0.005)
    ypad = max((maxy - miny) * 2, 0.005)

    map_bbox = (
        minx - xpad,
        miny - ypad,
        maxx + xpad,
        maxy + ypad
    )
#-------------------------------------------------------------------------------
    # Load neighboring parcels from every county shapefile.
    map_frames = []

    for shp in sorted(shp_files):
        source_crs = pyogrio.read_info(shp)["crs"]

        if CRS.from_user_input(source_crs) != CRS.from_epsg(4326):
            source_transformer = Transformer.from_crs(
                "EPSG:4326",
                source_crs,
                always_xy=True
            )
            source_bbox = transform(
                source_transformer.transform,
                box(*map_bbox)
            ).bounds
        else:
            source_bbox = map_bbox

        nearby_map = gpd.read_file(shp, bbox=source_bbox)

        if not nearby_map.empty:
            nearby_map = nearby_map.to_crs(epsg=4326)
            map_frames.append(nearby_map)

    if not map_frames:
        raise RuntimeError(
            "No neighboring parcel records found for map."
        )

    map_gdf = gpd.GeoDataFrame(
        pd.concat(map_frames, ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326"
    )
#-------------------------------------------------------------------------------
    print(f"Map-area parcels: {len(map_gdf):,}")

    create_map(
        map_gdf,
        parcel,
        lat,
        lon,
        county_name
    )

if __name__ == "__main__":
    main()