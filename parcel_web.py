#!/usr/bin/env python3
"""Local-network photo-to-parcel Flask prototype.

Run from the pyLatLong2ParcelMap project folder:
    python parcel_web.py
Then visit http://<debian-lan-ip>:5000 from a phone on the same Wi-Fi.

Requires Flask, ExifTool, and the existing pyPhoto2Parcel.py / mapper.
Do not expose this development server to the public internet.
"""

import contextlib
import io
import os
import secrets
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask, abort, render_template_string, request, send_file, url_for, Response
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from pyPhoto2Parcel import extract_coordinates, run_mapper

BASE = Path(__file__).resolve().parent
MAX_BYTES = 25 * 1024 * 1024
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES
app.config["SECRET_KEY"] = secrets.token_hex(32)

# The existing mapper uses fixed cache/output paths and creates a desktop image
# viewer. Run one map request at a time to avoid concurrent writes.
map_lock = threading.Lock()
results = {}  # token -> {path, details, timestamp}; local demo, memory only
RESULT_TTL = 60 * 60

# Require authentication before serving any page or processing uploads.
@app.before_request
def require_authentication():
    expected_password = os.environ.get("PARCEL_WEB_PASSWORD")

    # Fail closed if the password has not been configured.
    if not expected_password:
        return Response("Server authentication is not configured.", status=503)

    auth = request.authorization

    tester_password = os.environ.get("PARCEL_TEST_PASSWORD")

    admin_ok = (
        auth is not None
        and secrets.compare_digest(auth.username or "", "daniel")
        and secrets.compare_digest(auth.password or "", expected_password)
    )

    tester_ok = (
        auth is not None
        and bool(tester_password)
        and secrets.compare_digest(auth.username or "", "tester")
        and secrets.compare_digest(auth.password or "", tester_password)
    )

    if not (admin_ok or tester_ok):
        return Response(
            "Authentication required.",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Texas Photo-to-Parcel"'},
        )

PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Texas Photo-to-Parcel</title>
<!-- Leaflet interactive mapping library -->
<link
  rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@turf/turf@6.5.0/turf.min.js"></script>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#192a3b;background:#f2f5f8}
main{max-width:700px;margin:2rem auto;padding:1.5rem;background:white;border-radius:16px;box-shadow:0 3px 20px #0001}
h1{font-size:1.55rem}p{line-height:1.5}input{display:block;width:100%;margin:1rem 0;font-size:1rem}
button,.download{display:inline-block;border:0;border-radius:9px;background:#135e9a;color:white;padding:.85rem 1.15rem;font-weight:650;text-decoration:none;font-size:1rem;cursor:pointer}
.error{color:#a02020;background:#fff2f2;padding:1rem;border-radius:8px}.muted{color:#596a77;font-size:.92rem}
img{width:100%;height:auto;border-radius:9px;border:1px solid #ddd}
#interactive_map {
    width: 100%;
    height: 450px;
    border: 1px solid #ddd;
    border-radius: 9px;
    margin: 1rem 0;
}

#interactive_map {
    position: relative;
}

#center_dot {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 14px;
    height: 14px;
    background: #0066ff;
    border: 3px solid white;
    border-radius: 50%;
    box-shadow: 0 0 0 2px #0066ff, 0 2px 8px #0005;
    transform: translate(-50%, -50%);
    z-index: 1000;
    pointer-events: none;
}

pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:1rem;border-radius:8px}
.spinner{display:inline-block;width:18px;height:18px;border:3px solid #cbd5e1;border-top-color:#135e9a;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
#processing_status{margin-top:1rem;padding:1rem;background:#eef6fc;border-radius:9px}
#processing_status[hidden]{display:none}
button:disabled{opacity:.6;cursor:wait}
</style></head><body><main>
<h1>Texas Photo-to-Parcel</h1>
<p>Upload an <strong>original GPS-tagged photograph</strong>. Your existing Python program will identify the parcel and generate an ownership map.</p>
<form id="parcel_form" method="post" enctype="multipart/form-data">
<label for="photo"><strong>Select original photograph</strong></label>
<input id="photo" type="file" name="photo" accept="image/jpeg,image/heic,image/heif,image/tiff">
<input type="hidden" id="phone_lat" name="phone_lat">
<input type="hidden" id="phone_lon" name="phone_lon">
<input type="hidden" id="phone_accuracy" name="phone_accuracy">
<button type="button" id="location_button" onclick="usePhoneLocation()">Use My Current Location</button>
<p id="location_status" class="muted" role="status"></p>
<button type="submit" id="submit_button">Identify parcel</button>
<div id="processing_status" role="status" aria-live="polite" hidden>
  <p><span class="spinner"></span> <strong>Identifying parcel...</strong></p>
  <p id="processing_timer" class="muted">Elapsed time: 0 seconds</p>
  <p class="muted">Locating parcel records and generating your map. This may take a minute.</p>
</div>
</form>
<script>
document.getElementById("parcel_form").addEventListener("submit", function() {
    const button = document.getElementById("submit_button");
    const status = document.getElementById("processing_status");
    const timer = document.getElementById("processing_timer");

    status.hidden = false;
    button.disabled = true;
    button.textContent = "Processing...";

    const started = Date.now();
    setInterval(function() {
        const seconds = Math.floor((Date.now() - started) / 1000);
        timer.textContent = "Elapsed time: " + seconds + " seconds";
    }, 1000);
});

function usePhoneLocation() {
    const status = document.getElementById("location_status");
    const button = document.getElementById("location_button");

    if (!navigator.geolocation) {
        status.textContent = "Your browser does not support location services.";
        return;
    }

    button.disabled = true;
    status.textContent = "Requesting your phone's location...";

    navigator.geolocation.getCurrentPosition(
        function(position) {
            document.getElementById("phone_lat").value = position.coords.latitude;
            document.getElementById("phone_lon").value = position.coords.longitude;
            document.getElementById("phone_accuracy").value = position.coords.accuracy;
            status.textContent = "Phone GPS acquired. You can now identify the parcel.";
            button.disabled = false;
        },
        function(error) {
            document.getElementById("phone_lat").value = "";
            document.getElementById("phone_lon").value = "";
            document.getElementById("phone_accuracy").value = "";
            status.textContent = "Unable to get location: " + error.message;
            button.disabled = false;
        },
        {enableHighAccuracy: true, timeout: 15000, maximumAge: 0}
    );
}
</script>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if result %}
{% if result.match_type == "exact" %}
<p style="background:#e8f5e9;padding:1rem;border-radius:9px">
<strong>Exact parcel intersection</strong><br>
Your GPS point falls inside or on the boundary of this parcel.
</p>
{% elif result.match_type == "nearby" %}
<p style="background:#fff3cd;padding:1rem;border-radius:9px">
<strong>Nearby parcel — verify this result</strong><br>
Your GPS point falls outside this parcel.
The nearest parcel is {{ result.match_distance }} meters away.
This may not be the correct property.
</p>
{% else %}
<p class="muted">Parcel match method could not be determined.</p>
{% endif %}
<h2>GPS Location Acquired</h2>
<p class="muted">The GPS location is an initial estimate. Use the interactive map below to select and confirm the correct parcel.</p>
<p><strong>{{ result.gps_source }}:</strong> {{ result.lat }}, {{ result.lon }}</p>
{% if result.accuracy is not none %}
<p><strong>Reported GPS accuracy:</strong> ±{{ result.accuracy }} feet</p>
{% endif %}

<h2>Interactive Parcel Map</h2>
<p class="muted">Drag the map beneath the blue dot until it is positioned over the correct parcel. Review the ownership information, then click Confirm This Parcel.</p>
<div id="interactive_map"></div>
<p id="adjusted_coordinates" class="muted"></p>

<div id="selected_parcel_details">
    <p class="muted">Move the map to select a parcel.</p>
</div>

<button type="button" id="confirm_parcel" disabled>
    Confirm This Parcel
</button>

<button type="button" id="change_parcel" hidden>
    Change Selection
</button>

<p id="selection_status" class="muted"></p>

<script>
(function() {
    const lat = {{ result.lat | tojson }};
    const lon = {{ result.lon | tojson }};
    const geojsonUrl = {{ url_for('get_geojson', token=result.token) | tojson }};

    const map = L.map("interactive_map").setView(
        [Number(lat), Number(lon)], 17
    );

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19
    }).addTo(map);

    // Original GPS location, preserved for reference.
    const originalGps = L.latLng(Number(lat), Number(lon));

    // Fixed blue dot centered over the map.
    const centerDot = document.createElement("div");
    centerDot.id = "center_dot";
    centerDot.title = "Move the map to position this dot over a parcel";

    document.getElementById("interactive_map").appendChild(centerDot);

    // Track the geographic coordinates underneath the fixed dot.
    function updateCenterPosition() {
        const center = map.getCenter();

        document.getElementById("adjusted_coordinates").textContent =
            "Selected location: " +
            center.lat.toFixed(7) + ", " +
            center.lng.toFixed(7);
    }

    map.on("moveend", updateCenterPosition);
    updateCenterPosition();
    // ---------------------------------------------------------
        const detailsBox = document.getElementById("selected_parcel_details");
    const confirmButton = document.getElementById("confirm_parcel");
    const changeButton = document.getElementById("change_parcel");
    const selectionStatus = document.getElementById("selection_status");

    let selectedFeature = null;
    let selectedLayer = null;
    let selectionLocked = false;

    function updateSelectedParcel(data, parcels) {
        if (selectionLocked) return;

        const center = map.getCenter();
        const point = turf.point([center.lng, center.lat]);

        let matchedFeature = null;

        for (const feature of data.features) {
            if (!feature.geometry) continue;

            if (turf.booleanPointInPolygon(point, feature)) {
                matchedFeature = feature;
                break;
            }
        }

        if (selectedLayer) {
            parcels.resetStyle(selectedLayer);
            selectedLayer = null;
        }

        selectedFeature = matchedFeature;

        if (!matchedFeature) {
            detailsBox.textContent =
                "No parcel found beneath the blue dot. Move the map to another property.";
            confirmButton.disabled = true;
            selectionStatus.textContent = "";
            return;
        }

        const p = matchedFeature.properties || {};

        detailsBox.innerHTML =
            "<strong>Owner:</strong> " + escapeHtml(p.OWNER_NAME) +
            "<br><strong>Property ID:</strong> " + escapeHtml(p.PROP_ID) +
            "<br><strong>Geo ID:</strong> " + escapeHtml(p.GEO_ID) +
            "<br><strong>Address:</strong> " + escapeHtml(p.SITUS_ADDR);

        parcels.eachLayer(function(layer) {
            if (layer.feature === matchedFeature) {
                selectedLayer = layer;
                layer.setStyle({
                    color: "red",
                    weight: 3,
                    fillColor: "#fff5a5",
                    fillOpacity: 0.45
                });
            }
        });

        confirmButton.disabled = false;
        selectionStatus.textContent = "Move the map to change the selected parcel.";
    }

    fetch(geojsonUrl)
        .then(response => {
            if (!response.ok) throw new Error("GeoJSON request failed");
            return response.json();
        })
        .then(data => {
            const parcels = L.geoJSON(data, {
                style: {
                    color: "#555",
                    weight: 1,
                    fillColor: "#3388ff",
                    fillOpacity: 0.08
                },
                onEachFeature: function(feature, layer) {
                    const p = feature.properties || {};

                    layer.bindPopup(
                        "<strong>Owner:</strong> " + escapeHtml(p.OWNER_NAME) +
                        "<br><strong>Property ID:</strong> " + escapeHtml(p.PROP_ID) +
                        "<br><strong>Geo ID:</strong> " + escapeHtml(p.GEO_ID) +
                        "<br><strong>Address:</strong> " + escapeHtml(p.SITUS_ADDR)
                    );
                }
            }).addTo(map);

            // Keep the original GPS position centered initially.
            map.setView(originalGps, 17);

            map.on("moveend", function() {
                updateSelectedParcel(data, parcels);
            });

            updateSelectedParcel(data, parcels);

             confirmButton.addEventListener("click", async function() {
                if (!selectedFeature || selectionLocked) return;

                const center = map.getCenter();
                const propId = selectedFeature.properties.PROP_ID;

                confirmButton.disabled = true;
                selectionStatus.textContent =
                    "Generating final parcel report and PNG...";

                try {
                    const response = await fetch(
                        "{{ url_for('confirm_parcel', token=result.token) }}",
                        {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json"
                            },
                            body: JSON.stringify({
                                lat: center.lat,
                                lon: center.lng,
                                prop_id: String(propId)
                            })
                        }
                    );

                    const result = await response.json();

                    if (!response.ok || !result.success) {
                        throw new Error(
                            result.error || "Parcel confirmation failed."
                        );
                    }

                    selectionLocked = true;

                    map.dragging.disable();
                    map.touchZoom.disable();
                    map.doubleClickZoom.disable();
                    map.scrollWheelZoom.disable();
                    map.boxZoom.disable();
                    map.keyboard.disable();

                    confirmButton.hidden = true;
                    changeButton.hidden = false;

                    const finalDetails =
                        document.getElementById("final_parcel_details");

                    finalDetails.textContent = result.details;

                    const finalMap =
                        document.getElementById("final_parcel_map");

                    const finalDownload =
                        document.getElementById("final_map_download");

                    const refreshedUrl =
                        result.map_url + "?v=" + Date.now();

                    finalMap.src = refreshedUrl;
                    finalDownload.href = refreshedUrl;

                    document.getElementById(
                        "final_parcel_result"
                    ).hidden = false;

                    selectionStatus.textContent =
                        "Parcel confirmed. Final report and PNG generated.";

                } catch (error) {
                    console.error("Confirmation error:", error);
                    selectionStatus.textContent =
                        "Confirmation failed: " + error.message;
                } finally {
                    if (!selectionLocked) {
                        confirmButton.disabled = false;
                    }
                }
            });

            changeButton.addEventListener("click", function() {
                selectionLocked = false;

                map.dragging.enable();
                map.touchZoom.enable();
                map.doubleClickZoom.enable();
                map.scrollWheelZoom.enable();
                map.boxZoom.enable();
                map.keyboard.enable();

                confirmButton.hidden = false;
                changeButton.hidden = true;

                updateSelectedParcel(data, parcels);
            });
        })
        .catch(error => {
            console.error("Interactive map error:", error);
            detailsBox.textContent =
                "Unable to load parcel boundaries. Please try again.";
        });
    //------------------------------------------------
    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }
})();
</script>

<div id="final_parcel_result" hidden>
    <h2>Confirmed Parcel Report</h2>

    <pre id="final_parcel_details"></pre>

    <h2>Final Parcel Map</h2>

    <a
        id="final_map_download"
        class="download"
        href="#"
        download="parcel_map.png"
    >
        Save parcel map (PNG)
    </a>

    <p>
        <img
            id="final_parcel_map"
            alt="Confirmed parcel ownership map"
        >
    </p>
</div>
{% endif %}
<p class="muted">Local-network prototype. GPS identifies the camera position, which may not be the photographed property. Appraisal/GIS ownership is preliminary, not a title opinion. Do not publish this server to the internet.</p>
</main></body></html>"""


def purge_old_results():
    now = time.time()
    for token, item in list(results.items()):
        if now - item["timestamp"] > RESULT_TTL:
            results.pop(token, None)


def parcel_details_from_output(output: str, png: Path) -> str:
    """Extract the fields already printed by the existing mapping script."""
    fields = ("OWNER_NAME", "PROP_ID", "GEO_ID", "LEGAL_AREA", "LEGAL_DESC", "SITUS_ADDR")
    found = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in fields:
            found[key.strip()] = value.strip()
    if found:
        return "\n".join(f"{field}: {found[field]}" for field in fields if field in found)
    return f"Map: {png.name}"


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return render_template_string(PAGE, error="File exceeds 25 MB. Please choose a smaller original photo.", result=None), 413


@app.route("/", methods=["GET", "POST"])
def index():
    purge_old_results()
    if request.method == "GET":
        return render_template_string(PAGE, error=None, result=None)

    uploaded = request.files.get("photo")
    has_photo = bool(uploaded and uploaded.filename)

    phone_lat = request.form.get("phone_lat", "").strip()
    phone_lon = request.form.get("phone_lon", "").strip()
    phone_accuracy = request.form.get("phone_accuracy", "").strip()

    if not has_photo and not (phone_lat and phone_lon):
        return render_template_string(
            PAGE,
            error="Select a photograph or tap 'Use My Current Location'.",
            result=None,
        ), 400

    suffix = ""
    if has_photo:
        suffix = Path(secure_filename(uploaded.filename)).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff"}:
            return render_template_string(
                PAGE,
                error="Please upload a JPEG, HEIC, HEIF, or TIFF photograph.",
                result=None,
            ), 400

    try:
        with TemporaryDirectory(prefix="parcel_photo_") as tmp:
            gps_source = "Phone GPS"

            if has_photo:
                photo_path = Path(tmp) / ("upload" + suffix)
                uploaded.save(photo_path)

                try:
                    lat, lon = extract_coordinates(photo_path)
                    gps_source = "Photo GPS"
                except ValueError:
                    if not (phone_lat and phone_lon):
                        raise ValueError(
                            "This photograph has no GPS coordinates. "
                            "Tap 'Use My Current Location' and try again."
                        )

            if gps_source == "Phone GPS":
                try:
                    lat = float(phone_lat)
                    lon = float(phone_lon)
                except ValueError:
                    raise ValueError("Invalid phone GPS coordinates.")

                import math
                if not (
                    math.isfinite(lat)
                    and math.isfinite(lon)
                    and -90 <= lat <= 90
                    and -180 <= lon <= 180
                ):
                    raise ValueError("Invalid phone GPS coordinates.")

            if not map_lock.acquire(blocking=False):
                return render_template_string(PAGE, error="Another map is being generated. Please try again shortly.", result=None), 429
            try:
                log = io.StringIO()
                with contextlib.redirect_stdout(log):
                    map_path, geojson_path = run_mapper(
                        lat, lon, include_geojson=True
                    )
                output = log.getvalue()
                app.logger.info("Map generation completed: %s", map_path.name)
                # The existing mapper overwrites maps for the same parcel. Make a
                # separate snapshot per request so links remain stable.
                token = secrets.token_urlsafe(24)
                snapshot_dir = BASE / "web_results"
                snapshot_dir.mkdir(exist_ok=True)
                snapshot = snapshot_dir / f"{token}.png"
                import shutil
                shutil.copy2(map_path, snapshot)

                geojson_snapshot = snapshot_dir / f"{token}.geojson"
                shutil.copy2(geojson_path, geojson_snapshot)

                results[token] = {
                    "path": snapshot,
                    "geojson_path": geojson_snapshot,
                    "timestamp": time.time(),
                }
            finally:
                map_lock.release()
        import re
        match_type = "unknown"
        match_distance = None

        if "Exact parcel match found." in output:
            match_type = "exact"
        else:
            match = re.search(
                r"Nearest parcel distance: ([0-9.]+) meters",
                output
            )
            if match:
                match_type = "nearby"
                match_distance = match.group(1)

        accuracy_display = None
        if gps_source == "Phone GPS" and phone_accuracy:
            try:
                import math
                accuracy_value = float(phone_accuracy)
                if math.isfinite(accuracy_value) and accuracy_value >= 0:
                    accuracy_display = f"{accuracy_value * 3.28084:.1f}"
            except ValueError:
                pass

        result = {"accuracy": accuracy_display, "match_type": match_type, "match_distance": match_distance, "lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "gps_source": gps_source, "details": parcel_details_from_output(output, map_path), "token": token}
        return render_template_string(PAGE, error=None, result=result)
    except (RuntimeError, OSError, ValueError) as exc:
        app.logger.exception("Photo processing failed")
        return render_template_string(PAGE, error=str(exc), result=None), 400

@app.route("/confirm/<token>", methods=["POST"])
def confirm_parcel(token):
    purge_old_results()

    item = results.get(token)
    if item is None:
        abort(404)

    data = request.get_json(silent=True) or {}

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        prop_id = str(data["prop_id"])

        import math
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
            and prop_id.strip()
        ):
            raise ValueError("Invalid confirmed parcel selection.")

        if not map_lock.acquire(blocking=False):
            return {"error": "Another map is being generated. Please try again."}, 429

        try:
            log = io.StringIO()

            with contextlib.redirect_stdout(log):
                map_path = run_mapper(
                    lat,
                    lon,
                    exact_only=True,
                    prop_id=prop_id,
                )

            output = log.getvalue()

            import shutil
            shutil.copy2(map_path, item["path"])

            item["details"] = parcel_details_from_output(
                output, map_path
            )
            item["confirmed"] = True

        finally:
            map_lock.release()

        return {
            "success": True,
            "details": item["details"],
            "map_url": f"/map/{token}",
        }

    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        app.logger.exception("Parcel confirmation failed")
        return {"error": str(exc)}, 400
@app.route("/map/<token>")
def get_map(token):
    purge_old_results()
    item = results.get(token)
    if item is None or not item["path"].is_file():
        abort(404)
    return send_file(item["path"], mimetype="image/png", as_attachment=False, download_name="parcel_map.png")

@app.route("/geojson/<token>")
def get_geojson(token):
    purge_old_results()

    item = results.get(token)

    if item is None:
        abort(404)

    geojson_path = item.get("geojson_path")

    if geojson_path is None or not geojson_path.is_file():
        abort(404)

    return send_file(
        geojson_path,
        mimetype="application/geo+json",
        as_attachment=False
    )

if __name__ == "__main__":
    print("Photo-to-parcel web app running on port 5000")
    print("Open http://<your-debian-computer-LAN-IP>:5000 on your phone")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
