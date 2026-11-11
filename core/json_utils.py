import json
import math
import random
import re
from datetime import datetime
from typing import Any, List, Optional, Tuple
from urllib.parse import quote, quote_plus


def safe_json_decode(raw_data: Any) -> Any:
    """Port of Flutter Utils.safeJsonDecode from lib/utils/utils.dart.

    Sanitizes invalid escape sequences (such as \\') often returned by the backend
    so JSON parsing behaves identically to the Flutter app.
    """
    if isinstance(raw_data, (dict, list)):
        return raw_data
    if raw_data is None:
        return None
    raw = str(raw_data).strip()
    if not raw:
        return None
    sanitized = raw.replace(r"\'", "'")
    try:
        return json.loads(sanitized)
    except json.JSONDecodeError:
        # Strip unsupported backslash escapes (keep valid JSON escapes: ", \, /, b, f, n, r, t, u)
        fallback = re.sub(r'\\([^"\\/bfnrtu])', r"\1", sanitized)
        return json.loads(fallback)


def build_dart_url(base_url: str, path: str, query_items: Optional[List[Tuple[str, str]]] = None,
                   encode_component_keys: Optional[set] = None) -> str:
    """Build URL matching Dart's Dio().get(stringUrl) / Uri.parse encoding behavior.

    In Flutter, raw query values keep '/', ':', ',', '.', '-', '_' unencoded (Uri.parse),
    except fields explicitly passed through Uri.encodeQueryComponent (like Deskripsi in sales).
    """
    base = base_url.rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    if not query_items:
        return f"{base}{clean_path}"

    encode_component_keys = encode_component_keys or set()
    parts = []
    for k, v in query_items:
        val_str = "" if v is None else str(v)
        if k in encode_component_keys:
            encoded_val = quote_plus(val_str)
        else:
            # Keep '/', ':', ',', '-', '_', '.', '~' unencoded like Dart Uri.parse
            encoded_val = quote(val_str, safe="/:,.-_~")
        parts.append(f"{k}={encoded_val}")
    return f"{base}{clean_path}?{'&'.join(parts)}"


def parse_server_datetime(sekarang: str) -> datetime:
    """Parse 'Sekarang' string returned by /api/masuk1018 with fallback."""
    if not sekarang:
        return datetime.now()
    s = sekarang.strip()
    formats = [
        "%Y/%m/%d %H:%M:%S",
        "%y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.now()


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters matching Geolocator.distanceBetween."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def add_gps_jitter(lat: float, lon: float, max_meters: float = 35.0) -> Tuple[str, str]:
    """Generate realistic coordinates within max_meters of office coords."""
    dist = random.uniform(2.0, max_meters)
    bearing = random.uniform(0.0, 2 * math.pi)
    d_lat = (dist * math.cos(bearing)) / 111320.0
    cos_lat = math.cos(math.radians(lat)) or 1.0
    d_lon = (dist * math.sin(bearing)) / (111320.0 * cos_lat)
    return f"{lat + d_lat:.14f}", f"{lon + d_lon:.14f}"


def parse_version_score(ver_str: str) -> int:
    """Same algorithm as CekUpdate.dart lines 58-64."""
    score = 0
    for part in str(ver_str).strip().split("."):
        try:
            num = int(part)
        except ValueError:
            num = 0
        score = score * 100 + num
    return score
