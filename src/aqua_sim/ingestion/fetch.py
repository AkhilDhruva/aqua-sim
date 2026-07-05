"""Fetch a public USGS 3DEP elevation tile as a GeoTIFF.

Uses the USGS 3DEP dynamic ImageServer ``exportImage`` endpoint — public-domain,
no API key. Given a WGS84 bounding box it returns a float32 elevation GeoTIFF that
``ingestion.DEMSource`` ingests directly.

Note on egress: this reaches ``elevation.nationalmap.gov`` over HTTPS. In sandboxes
with a restrictive network policy that host may be blocked (HTTP 403 from the
egress proxy) — run this where outbound access to USGS is permitted. The rest of
the pipeline (DEMSource -> solver -> risk -> frames) runs fully offline once the
tile is on disk.
"""

from __future__ import annotations

import urllib.parse
import urllib.request

# A modest Manhattan bounding box (Lower/Mid), WGS84 (min_lon, min_lat, max_lon, max_lat).
MANHATTAN_BBOX = (-74.02, 40.70, -73.93, 40.78)

_BASE = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
         "3DEPElevation/ImageServer/exportImage")


def build_3dep_url(bbox: tuple[float, float, float, float], size: int = 1024) -> str:
    """Build the USGS 3DEP exportImage URL for a WGS84 ``bbox``."""
    params = {
        "bbox": ",".join(str(v) for v in bbox),
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": f"{size},{size}",
        "format": "tiff",
        "pixelType": "F32",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }
    return _BASE + "?" + urllib.parse.urlencode(params)


def fetch_3dep(
    out_path: str,
    bbox: tuple[float, float, float, float] = MANHATTAN_BBOX,
    size: int = 1024,
    timeout: int = 120,
) -> str:
    """Download a 3DEP elevation GeoTIFF for ``bbox`` to ``out_path``.

    Returns the output path. Raises on network/policy errors (e.g. a blocked host
    in a restricted sandbox) — catch and report; do not retry a policy denial.
    """
    url = build_3dep_url(bbox, size)
    req = urllib.request.Request(url, headers={"User-Agent": "aqua-sim/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read()
    if "tiff" not in ctype.lower():
        raise RuntimeError(f"Expected a GeoTIFF, got Content-Type={ctype!r} "
                           f"({len(data)} bytes). The service may have returned an error.")
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


if __name__ == "__main__":  # pragma: no cover - manual utility
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "manhattan_3dep.tif"
    print(f"Fetching USGS 3DEP tile for {MANHATTAN_BBOX} -> {out}")
    try:
        fetch_3dep(out)
        print(f"Wrote {out}")
    except Exception as e:  # noqa: BLE001
        print(f"Fetch failed ({type(e).__name__}): {e}")
        print("If this is a blocked-host error, run where egress to USGS is allowed.")
        raise SystemExit(1)
