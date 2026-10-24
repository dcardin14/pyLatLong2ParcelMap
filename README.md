# pyLatLong2ParcelMap — Debian Linux edition

Given a Texas latitude/longitude, identifies the county, downloads the county's TxGIO 2026 land parcel shapefile, finds the parcel containing the point, prints its property attributes, and saves a PNG parcel/aerial map.

## Install (Debian 12/13 or Debian-derived Linux)

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If the geospatial Python wheels fail to install, install native dependencies first:

```bash
sudo apt install build-essential libgdal-dev gdal-bin libproj-dev libgeos-dev python3-dev
```

## Run

```bash
python pyLatLong2ParcelMap.py '30.3119,-95.4561'
python pyLatLong2ParcelMap.py '30d18m42sN,95d27m22sW' --open
python pyLatLong2ParcelMap.py '30.3119,-95.4561' --no-aerial
```

PNG files go to `output/`. County parcel archives and extracted shapefiles are kept in `cache/`, so the same county does not need to be downloaded again. The program runs headlessly (including SSH) and **does not** open an image unless `--open` is requested. The aerial basemap needs an Internet connection; `--no-aerial` disables it.

The program normally looks up the county using the live **TxDOT ArcGIS Texas Counties** service. For offline county lookup, supply a county boundaries GeoJSON with the appropriate name/FIPS fields:

```bash
python pyLatLong2ParcelMap.py '30.3119,-95.4561' --county-file counties/txdot_counties.geojson
```

An offline county file does **not** eliminate the need to download the land parcels at least once.

## Test

```bash
python -m unittest discover -s tests -v
```

## Changes from the Windows-oriented script

- Removes Windows-only `os.startfile()`; `--open` optionally launches the default viewer on Debian.
- Avoids assuming the script is launched from a specific directory.
- Performs county lookup using TxDOT's point query rather than requiring a bundled county GeoJSON.
- Transforms coordinates into the actual parcel shapefile CRS before spatial selection.
- Uses non-interactive Matplotlib backend for desktops, terminals, and servers.
- Prevents ZIP path traversal and handles partial or invalid ZIP downloads.
- Handles missing attribute values; keeps maps and cached data out of version control.

## Notes

The remote TxGIO archive filename convention is retained from the original script; availability of each county's **2026** ZIP has not been verified. Some counties may be missing or use different available releases. A 404 means the requested file is unavailable at that URL, not that Debian is broken. TxGIO notes that parcel data are not survey-grade and should **not** be relied upon for legal title decisions.

Sources: [TxDOT county layer](https://maps.dot.state.tx.us/arcgis/rest/services/Boundaries/MapServer/1), [TxGIO land parcels](https://www.tnris.org/stratmap/land-parcels.html).
