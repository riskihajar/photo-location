#!/usr/bin/env python3
import argparse
import math
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont


EARTH_RADIUS_METERS = 6371008.8


def load_env(path):
    if not path.exists():
        return
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_api_key():
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_MAPS_API_KEY is missing. Put it in .env or export it.")
    return api_key


def run_command(args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_image_coordinates(image_path):
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(path)

    lat_value = run_command(["mdls", "-raw", "-name", "kMDItemLatitude", str(path)]).strip()
    lng_value = run_command(["mdls", "-raw", "-name", "kMDItemLongitude", str(path)]).strip()
    lat = parse_float(lat_value)
    lng = parse_float(lng_value)
    if lat is None or lng is None:
        raise ValueError(f"No GPS latitude/longitude metadata found in {path}.")
    return lat, lng


def resolve_coordinates(args):
    has_from = args.from_lat is not None and args.from_lng is not None
    has_to = args.to_lat is not None and args.to_lng is not None

    if any(value is not None for value in (args.from_lat, args.from_lng)) and not has_from:
        raise ValueError("Pass both --from-lat and --from-lng, or neither.")
    if any(value is not None for value in (args.to_lat, args.to_lng)) and not has_to:
        raise ValueError("Pass both --to-lat and --to-lng, or neither.")

    if has_from and has_to:
        return (args.from_lat, args.from_lng), (args.to_lat, args.to_lng), "custom coordinates"

    if args.image_a and args.image_b:
        return read_image_coordinates(args.image_a), read_image_coordinates(args.image_b), "image metadata"

    raise ValueError(
        "Provide either two images, or custom coordinates with "
        "--from-lat --from-lng --to-lat --to-lng."
    )


def haversine_distance_meters(point_a, point_b):
    lat1, lng1 = point_a
    lat2, lng2 = point_b
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_METERS * c


def initial_bearing_degrees(point_a, point_b):
    lat1, lng1 = map(math.radians, point_a)
    lat2, lng2 = map(math.radians, point_b)
    delta_lng = lng2 - lng1
    x = math.sin(delta_lng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def format_distance(meters):
    if meters >= 1000:
        return f"{meters / 1000:.3f} km"
    return f"{meters:.1f} m"


def parse_size(size):
    try:
        width, height = size.lower().split("x", 1)
        return int(width), int(height)
    except ValueError as exc:
        raise ValueError("Invalid --size. Use WIDTHxHEIGHT, e.g. 457x640.") from exc


def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates.extend(
            [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
            ]
        )
    candidates.extend(
        [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Helvetica.ttf",
            "/Library/Fonts/Arial.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def google_get_json(url, params):
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "OK":
        raise RuntimeError(data.get("error_message") or str(data)[:500])
    return data


def reverse_geocode(api_key, point, language):
    lat, lng = point
    data = google_get_json(
        "https://maps.googleapis.com/maps/api/geocode/json",
        {"latlng": f"{lat},{lng}", "key": api_key, "language": language},
    )
    result = data["results"][0]
    components = result.get("address_components", [])
    return {
        "title": build_location_title(components),
        "address": result.get("formatted_address", ""),
    }


def get_route_info(api_key, point_a, point_b, route_mode):
    straight_distance = haversine_distance_meters(point_a, point_b)
    if route_mode == "straight":
        lat1, lng1 = point_a
        lat2, lng2 = point_b
        return {
            "distance_m": straight_distance,
            "path": f"{lat1},{lng1}|{lat2},{lng2}",
            "label": "Garis lurus",
        }

    lat1, lng1 = point_a
    lat2, lng2 = point_b
    data = google_get_json(
        "https://maps.googleapis.com/maps/api/directions/json",
        {
            "origin": f"{lat1},{lng1}",
            "destination": f"{lat2},{lng2}",
            "mode": route_mode,
            "alternatives": "false",
            "key": api_key,
        },
    )
    route = data["routes"][0]
    distance_m = sum(leg.get("distance", {}).get("value", 0) for leg in route.get("legs", []))
    encoded_polyline = route.get("overview_polyline", {}).get("points")
    if not encoded_polyline:
        raise RuntimeError("Directions API did not return an overview polyline.")
    return {
        "distance_m": distance_m or straight_distance,
        "path": f"enc:{encoded_polyline}",
        "label": route_mode.capitalize(),
    }


def build_location_title(components):
    city = component_value(
        components,
        ["locality", "administrative_area_level_2", "sublocality", "postal_town"],
    )
    province = component_value(components, ["administrative_area_level_1"])
    country = component_value(components, ["country"])
    parts = [part for part in (city, province, country) if part]
    return ", ".join(parts) if parts else "Unknown Location"


def component_value(components, types):
    for wanted in types:
        for component in components:
            if wanted in component.get("types", []):
                return component.get("long_name", "")
    return ""


def download_static_route_map(api_key, point_a, point_b, route_path, size, scale, maptype):
    lat1, lng1 = point_a
    lat2, lng2 = point_b
    response = requests.get(
        "https://maps.googleapis.com/maps/api/staticmap",
        params={
            "size": size,
            "scale": scale,
            "maptype": maptype,
            "path": f"color:0x0B72F6FF|weight:6|{route_path}",
            "markers": [
                f"color:green|label:A|{lat1},{lng1}",
                f"color:red|label:B|{lat2},{lng2}",
            ],
            "key": api_key,
        },
        timeout=30,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise RuntimeError(response.text[:500])

    return response.content


def render_route_report(
    api_key,
    point_a,
    point_b,
    route_path,
    route_label,
    output_path,
    size,
    scale,
    maptype,
    distance_m,
    bearing,
    location_a,
    location_b,
):
    final_width, final_height = parse_size(size)
    final_width *= scale
    final_height *= scale
    padding = max(24, int(final_width * 0.04))
    title_font = load_font(max(32, int(final_width * 0.041)), bold=True)
    label_font = load_font(max(21, int(final_width * 0.025)), bold=True)
    body_font = load_font(max(18, int(final_width * 0.022)), bold=False)
    small_font = load_font(max(17, int(final_width * 0.020)), bold=False)

    measure_canvas = Image.new("RGB", (final_width, final_height), (17, 23, 29))
    measure_draw = ImageDraw.Draw(measure_canvas)
    content_width = final_width - padding * 2
    location_a_height = measure_location_block_height(measure_draw, location_a, content_width, label_font, body_font, small_font)
    location_b_height = measure_location_block_height(measure_draw, location_b, content_width, label_font, body_font, small_font)
    header_height = int(title_font.size * 2.0)
    block_gap = int(body_font.size * 0.55)
    panel_height = padding * 2 + header_height + location_a_height + block_gap + location_b_height
    panel_height = min(max(panel_height, int(final_height * 0.20)), int(final_height * 0.32))
    map_height = final_height - panel_height
    request_size = f"{final_width // scale}x{map_height // scale}"
    map_bytes = download_static_route_map(api_key, point_a, point_b, route_path, request_size, scale, maptype)
    map_image = Image.open(BytesIO(map_bytes)).convert("RGB")
    map_image = map_image.resize((final_width, map_height), Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", (final_width, final_height), (17, 23, 29))
    canvas.paste(map_image, (0, 0))

    draw = ImageDraw.Draw(canvas)

    panel_y = map_height
    draw.rectangle((0, panel_y, final_width, final_height), fill=(17, 23, 29))
    draw.line((0, panel_y, final_width, panel_y), fill=(255, 255, 255), width=2)

    x = padding
    y = panel_y + padding
    distance_text = f"Panjang jalan: {format_distance(distance_m)}"
    draw.text((x, y), distance_text, fill=(255, 255, 255), font=title_font)
    bearing_text = f"Rute: {route_label} | Bearing A ke B: {bearing:.1f} deg"
    draw.text((x, y + int(title_font.size * 1.25)), bearing_text, fill=(193, 216, 232), font=small_font)

    column_width = content_width
    left_x = padding
    section_y = y + int(title_font.size * 2.2)
    next_y = draw_location_block(draw, "A", location_a, point_a, left_x, section_y, column_width, label_font, body_font, small_font)
    draw_location_block(draw, "B", location_b, point_b, left_x, next_y + block_gap, column_width, label_font, body_font, small_font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, optimize=True)


def draw_location_block(draw, label, location, point, x, y, max_width, label_font, body_font, small_font):
    lat, lng = point
    draw.text((x, y), f"Titik {label}: {location['title']}", fill=(255, 255, 255), font=label_font)
    y += int(label_font.size * 1.25)
    address_lines = wrap_text(draw, location["address"], body_font, max_width)[:2]
    for line in address_lines:
        draw.text((x, y), line, fill=(220, 227, 232), font=body_font)
        y += int(body_font.size * 1.22)
    y += max(4, int(small_font.size * 0.25))
    draw.text((x, y), f"Lat {lat:.6f}, Long {lng:.6f}", fill=(180, 225, 255), font=small_font)
    return y + int(small_font.size * 1.3)


def measure_location_block_height(draw, location, max_width, label_font, body_font, small_font):
    address_lines = wrap_text(draw, location["address"], body_font, max_width)[:2]
    return (
        int(label_font.size * 1.25)
        + len(address_lines) * int(body_font.size * 1.22)
        + max(4, int(small_font.size * 0.25))
        + int(small_font.size * 1.3)
    )


def build_default_output_path(image_a, image_b):
    if image_a and image_b:
        return Path("output") / f"{Path(image_a).stem}_to_{Path(image_b).stem}_map.png"
    return Path("output") / "distance_map.png"


def main():
    parser = argparse.ArgumentParser(description="Render a 5:7 Google map with a line between two photo locations.")
    parser.add_argument("image_a", nargs="?", help="First image path; GPS read from metadata")
    parser.add_argument("image_b", nargs="?", help="Second image path; GPS read from metadata")
    parser.add_argument("--from-lat", type=float, help="Start latitude")
    parser.add_argument("--from-lng", type=float, help="Start longitude")
    parser.add_argument("--to-lat", type=float, help="End latitude")
    parser.add_argument("--to-lng", type=float, help="End longitude")
    parser.add_argument("-o", "--output", help="Output PNG path")
    parser.add_argument("--size", default="457x640", help="Static map size before scale, default: 457x640 (5:7)")
    parser.add_argument("--scale", type=int, default=2, choices=[1, 2], help="Google Static Maps scale, default: 2")
    parser.add_argument("--maptype", default="roadmap", choices=["roadmap", "satellite", "hybrid", "terrain"])
    parser.add_argument("--language", default="en", help="Google geocoding language, default: en")
    parser.add_argument(
        "--route",
        default="driving",
        choices=["driving", "walking", "bicycling", "straight"],
        help="Route path type, default: driving. Use straight for direct line.",
    )
    args = parser.parse_args()

    load_env(Path(".env"))
    api_key = require_api_key()
    point_a, point_b, source = resolve_coordinates(args)
    route_info = get_route_info(api_key, point_a, point_b, args.route)
    distance_m = route_info["distance_m"]
    bearing = initial_bearing_degrees(point_a, point_b)
    location_a = reverse_geocode(api_key, point_a, args.language)
    location_b = reverse_geocode(api_key, point_b, args.language)
    output_path = Path(args.output) if args.output else build_default_output_path(args.image_a, args.image_b)

    render_route_report(
        api_key,
        point_a,
        point_b,
        route_info["path"],
        route_info["label"],
        output_path,
        args.size,
        args.scale,
        args.maptype,
        distance_m,
        bearing,
        location_a,
        location_b,
    )

    print(f"Source: {source}")
    print(f"Point A: Lat {point_a[0]:.7f}, Long {point_a[1]:.7f}")
    print(f"Location A: {location_a['title']}")
    print(f"Point B: Lat {point_b[0]:.7f}, Long {point_b[1]:.7f}")
    print(f"Location B: {location_b['title']}")
    print(f"Route: {route_info['label']}")
    print(f"Distance: {format_distance(distance_m)} ({distance_m:.2f} m)")
    print(f"Bearing: {bearing:.1f} deg")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
