#!/usr/bin/env python3
import argparse
import calendar
import io
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


MDLS_KEYS = [
    "kMDItemLatitude",
    "kMDItemLongitude",
    "kMDItemAltitude",
    "kMDItemContentCreationDate",
    "kMDItemAcquisitionMake",
    "kMDItemAcquisitionModel",
]

DAY_NAMES_ID = [
    "Senin",
    "Selasa",
    "Rabu",
    "Kamis",
    "Jumat",
    "Sabtu",
    "Minggu",
]


def load_env(path):
    if not path.exists():
        return
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def run_command(args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def read_mdls_metadata(image_path):
    metadata = {}
    for key in MDLS_KEYS:
        value = run_command(["mdls", "-raw", "-name", key, str(image_path)]).strip()
        if value == "(null)":
            value = ""
        metadata[key] = value

    lat = parse_float(metadata["kMDItemLatitude"])
    lng = parse_float(metadata["kMDItemLongitude"])

    return {
        "latitude": lat,
        "longitude": lng,
        "altitude": parse_float(metadata["kMDItemAltitude"]),
        "created_utc": parse_mdls_date(metadata["kMDItemContentCreationDate"]),
        "make": metadata["kMDItemAcquisitionMake"],
        "model": metadata["kMDItemAcquisitionModel"],
    }


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_mdls_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def parse_custom_datetime(value):
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
        return parsed
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    raise ValueError(
        "Invalid date format. Use ISO format like '2026-04-24 17:22' "
        "or include timezone like '2026-04-24T17:22:00+08:00'."
    )


def to_utc_datetime(value, tz_info):
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    offset = timezone(timedelta(seconds=tz_info["offset_seconds"]))
    return value.replace(tzinfo=offset).astimezone(timezone.utc)


def require_api_key():
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_MAPS_API_KEY is missing. Put it in .env or export it.")
    return api_key


def google_get_json(url, params):
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    status = data.get("status")
    if status != "OK":
        message = data.get("error_message") or json.dumps(data)[:500]
        raise RuntimeError(f"Google API failed: {status}: {message}")
    return data


def reverse_geocode(api_key, lat, lng, language):
    data = google_get_json(
        "https://maps.googleapis.com/maps/api/geocode/json",
        {"latlng": f"{lat},{lng}", "key": api_key, "language": language},
    )
    result = data["results"][0]
    components = result.get("address_components", [])
    return {
        "formatted_address": result.get("formatted_address", ""),
        "title": build_location_title(components),
        "components": components,
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


def get_timezone_info(api_key, lat, lng, created_utc):
    timestamp = int(created_utc.timestamp()) if created_utc else calendar.timegm(datetime.now().utctimetuple())
    data = google_get_json(
        "https://maps.googleapis.com/maps/api/timezone/json",
        {"location": f"{lat},{lng}", "timestamp": timestamp, "key": api_key},
    )
    offset_seconds = int(data.get("rawOffset", 0)) + int(data.get("dstOffset", 0))
    return {
        "time_zone_id": data.get("timeZoneId", ""),
        "time_zone_name": data.get("timeZoneName", ""),
        "offset_seconds": offset_seconds,
    }


def download_static_map(api_key, lat, lng, output_path, size="480x480", zoom=18):
    response = requests.get(
        "https://maps.googleapis.com/maps/api/staticmap",
        params={
            "center": f"{lat},{lng}",
            "zoom": zoom,
            "size": size,
            "scale": 2,
            "maptype": "roadmap",
            "markers": f"color:red|{lat},{lng}",
            "key": api_key,
        },
        timeout=30,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise RuntimeError(response.text[:500])
    output_path.write_bytes(response.content)


def convert_to_jpeg(source_path, output_path):
    run_command(["magick", str(source_path), "-auto-orient", str(output_path)])


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


def format_timestamp(created_utc, tz_info):
    if not created_utc:
        created_utc = datetime.now(timezone.utc)
    offset = timezone(timedelta(seconds=tz_info["offset_seconds"]))
    local_time = created_utc.astimezone(offset)
    day = DAY_NAMES_ID[local_time.weekday()]
    gmt = format_gmt_offset(tz_info["offset_seconds"])
    return f"{day}, {local_time:%d/%m/%Y %H:%M} {gmt}"


def format_gmt_offset(offset_seconds):
    sign = "+" if offset_seconds >= 0 else "-"
    seconds = abs(offset_seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"GMT {sign}{hours:02d}:{minutes:02d}"


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


def text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def rounded_rectangle(draw, xy, radius, fill):
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def render_overlay(image_path, map_path, overlay_data, output_path):
    image = Image.open(image_path).convert("RGB")
    image = ImageOps.exif_transpose(image)
    base = image.convert("RGBA")

    width, height = base.size
    margin = max(36, int(width * 0.035))
    card_width = width - (margin * 2)
    padding = max(32, int(width * 0.022))
    radius = max(14, int(width * 0.009))

    title_font = load_font(max(54, int(width * 0.028)), bold=True)
    body_font = load_font(max(40, int(width * 0.021)), bold=False)
    mono_font = load_font(max(36, int(width * 0.019)), bold=False)

    map_image = Image.open(map_path).convert("RGB")
    temp_draw = ImageDraw.Draw(base)
    text_x_gap = padding
    line_gap = max(8, int(width * 0.004))
    section_gap = max(14, int(width * 0.006))
    title_line_height = int(title_font.size * 1.2)
    body_line_height = int(body_font.size * 1.22)
    mono_line_height = int(mono_font.size * 1.2)

    def measure_text_layout(current_map_width):
        current_text_width_max = card_width - (padding * 3) - current_map_width - text_x_gap
        current_title_lines = wrap_text(temp_draw, overlay_data["title"], title_font, current_text_width_max)
        current_address_lines = wrap_text(temp_draw, overlay_data["address"], body_font, current_text_width_max)[:2]
        current_coordinate_lines = wrap_text(temp_draw, overlay_data["coordinates"], mono_font, current_text_width_max)
        current_timestamp_lines = wrap_text(temp_draw, overlay_data["timestamp"], body_font, current_text_width_max)
        current_text_height = (
            len(current_title_lines) * title_line_height
            + section_gap
            + len(current_address_lines) * body_line_height
            + section_gap
            + len(current_coordinate_lines) * mono_line_height
            + line_gap
            + len(current_timestamp_lines) * body_line_height
        )
        return (
            current_title_lines,
            current_address_lines,
            current_coordinate_lines,
            current_timestamp_lines,
            current_text_height,
        )

    map_width = min(int(card_width * 0.32), 1100)
    for _ in range(3):
        title_lines, address_lines, coordinate_lines, timestamp_lines, text_height = measure_text_layout(map_width)
        next_map_width = min(max(text_height, int(card_width * 0.25)), int(card_width * 0.34), 1100)
        if abs(next_map_width - map_width) < 8:
            map_width = next_map_width
            break
        map_width = next_map_width

    title_lines, address_lines, coordinate_lines, timestamp_lines, text_height = measure_text_layout(map_width)

    map_height = text_height
    map_width = map_height
    map_image = ImageOps.fit(map_image, (map_width, map_height), method=Image.Resampling.LANCZOS)

    card_height = text_height + padding * 2
    card_x = margin
    card_y = height - card_height - margin

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    shadow_offset = max(8, int(width * 0.005))
    rounded_rectangle(
        draw,
        (card_x + shadow_offset, card_y + shadow_offset, card_x + card_width + shadow_offset, card_y + card_height + shadow_offset),
        radius,
        (0, 0, 0, 90),
    )
    rounded_rectangle(
        draw,
        (card_x, card_y, card_x + card_width, card_y + card_height),
        radius,
        (16, 22, 28, 218),
    )

    map_x = card_x + padding
    map_y = card_y + padding
    mask = Image.new("L", (map_width, map_height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, map_width, map_height), radius=max(8, radius // 2), fill=255)
    overlay.paste(map_image.convert("RGBA"), (map_x, map_y), mask)

    draw = ImageDraw.Draw(overlay)
    text_x = map_x + map_width + text_x_gap
    text_y = card_y + padding
    for line in title_lines:
        draw.text((text_x, text_y), line, fill=(255, 255, 255, 255), font=title_font)
        text_y += title_line_height
    text_y += section_gap
    for line in address_lines:
        draw.text((text_x, text_y), line, fill=(218, 226, 232, 255), font=body_font)
        text_y += body_line_height
    text_y += section_gap
    for line in coordinate_lines:
        draw.text((text_x, text_y), line, fill=(190, 231, 255, 255), font=mono_font)
        text_y += mono_line_height
    text_y += line_gap
    for line in timestamp_lines:
        draw.text((text_x, text_y), line, fill=(236, 240, 242, 255), font=body_font)
        text_y += body_line_height

    final = Image.alpha_composite(base, overlay).convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(output_path, quality=92, optimize=True)


def build_output_path(input_path, output_arg):
    if output_arg:
        return Path(output_arg)
    return Path("output") / f"{input_path.stem}_location_overlay.jpg"


def main():
    parser = argparse.ArgumentParser(description="Generate a location overlay image from photo metadata.")
    parser.add_argument("image", help="Input image path, including HEIC/HEIF/JPG/PNG")
    parser.add_argument("-o", "--output", help="Output image path")
    parser.add_argument("--lat", type=float, help="Custom latitude when image has no GPS metadata")
    parser.add_argument("--lng", type=float, help="Custom longitude when image has no GPS metadata")
    parser.add_argument(
        "--date",
        help="Custom capture date, e.g. '2026-04-24 17:22' or '2026-04-24T17:22:00+08:00'",
    )
    parser.add_argument("--language", default="en", help="Google geocoding language, default: en")
    parser.add_argument("--zoom", type=int, default=18, help="Google static map zoom, default: 18")
    args = parser.parse_args()

    if (args.lat is None) != (args.lng is None):
        raise ValueError("Pass both --lat and --lng, or neither.")

    input_path = Path(args.image)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    load_env(Path(".env"))
    api_key = require_api_key()

    metadata = read_mdls_metadata(input_path)
    lat = args.lat if args.lat is not None else metadata["latitude"]
    lng = args.lng if args.lng is not None else metadata["longitude"]
    if lat is None or lng is None:
        raise ValueError("No GPS metadata found. Pass custom coordinates with --lat and --lng.")

    custom_datetime = parse_custom_datetime(args.date)
    metadata_datetime = metadata["created_utc"]
    capture_datetime = custom_datetime if custom_datetime is not None else metadata_datetime

    geocode = reverse_geocode(api_key, lat, lng, args.language)
    tz_info = get_timezone_info(api_key, lat, lng, capture_datetime)
    capture_utc = to_utc_datetime(capture_datetime, tz_info)
    if custom_datetime is not None and custom_datetime.tzinfo is None:
        tz_info = get_timezone_info(api_key, lat, lng, capture_utc)

    output_path = build_output_path(input_path, args.output)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        converted_path = temp_dir_path / "input.jpg"
        map_path = temp_dir_path / "map.png"
        convert_to_jpeg(input_path, converted_path)
        download_static_map(api_key, lat, lng, map_path, zoom=args.zoom)

        overlay_data = {
            "title": geocode["title"],
            "address": geocode["formatted_address"],
            "coordinates": f"Lat {lat:.6f}, Long {lng:.6f}",
            "timestamp": format_timestamp(capture_utc, tz_info),
        }
        render_overlay(converted_path, map_path, overlay_data, output_path)

    print(f"Input: {input_path}")
    print(f"Location: {overlay_data['title']}")
    print(f"Address: {overlay_data['address']}")
    print(f"Coordinates: {overlay_data['coordinates']}")
    print(f"Timestamp: {overlay_data['timestamp']}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
