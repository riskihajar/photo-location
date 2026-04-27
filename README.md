# Photo Location Tools

Small Python utilities for field-report images:

- Generate a location overlay on top of an existing photo.
- Render a 5:7 map report with a line between two photo locations.
- Read GPS metadata from HEIC/HEIF/JPG images on macOS.
- Fallback to custom latitude/longitude when metadata is missing.

## Requirements

- macOS, because metadata extraction currently uses `mdls`.
- Python 3.12+
- ImageMagick CLI, available as `magick`.
- Python packages:
  - `Pillow`
  - `requests`
- Google Maps Platform API key with these APIs enabled:
  - Geocoding API
  - Time Zone API
  - Maps Static API
  - Directions API

## Environment

Create `.env` in the project root:

```bash
GOOGLE_MAPS_API_KEY=your_google_maps_api_key
```

The `.env` file is ignored by git.

## Generate Photo Location Overlay

Use this for a single existing photo. The script reads photo GPS metadata, looks up the address, downloads a map thumbnail, and renders a caption overlay.

```bash
source ~/.zshrc && python3 generate_location_overlay.py IMG_7163.HEIC
```

Default output:

```text
output/IMG_7163_location_overlay.jpg
```

### Custom Coordinates

Use custom coordinates when the photo has no GPS metadata, or when you want to override it.

```bash
source ~/.zshrc && python3 generate_location_overlay.py IMG_7163.HEIC \
  --lat -0.5204814 \
  --lng 117.1706029
```

### Custom Date

If metadata has no capture date, the script falls back to the current date/time. You can override it with `--date`.

```bash
source ~/.zshrc && python3 generate_location_overlay.py IMG_7163.HEIC \
  --lat -0.5204814 \
  --lng 117.1706029 \
  --date "2026-04-27 10:21"
```

With explicit timezone:

```bash
source ~/.zshrc && python3 generate_location_overlay.py IMG_7163.HEIC \
  --lat -0.5204814 \
  --lng 117.1706029 \
  --date "2026-04-27T10:21:00+08:00"
```

### Useful Options

```bash
python3 generate_location_overlay.py --help
```

- `--language id` for Indonesian Google address results.
- `--zoom 19` to make the map thumbnail more zoomed-in.
- `-o output/custom.jpg` to choose output path.

## Render Distance Map Report

Use this for road-condition field reports where you need distance between two locations.

The output is a 5:7 PNG containing:

- map with route line from point A to point B
- marker A and marker B
- road length in meters
- bearing from A to B
- address and coordinates for both points

### From Custom Coordinates

```bash
source ~/.zshrc && python3 render_distance_map.py \
  --from-lat -0.519975 \
  --from-lng 117.1715783333333 \
  --to-lat -0.5204814 \
  --to-lng 117.1706029 \
  --route driving \
  -o output/distance_map_custom.png
```

By default, `--route driving` follows the road using Google Directions API. Use `--route straight` if you need a direct line only.

### From Two Photos

If both photos have GPS metadata:

```bash
source ~/.zshrc && python3 render_distance_map.py IMG_A.HEIC IMG_B.HEIC
```

Default output:

```text
output/IMG_A_to_IMG_B_map.png
```

### Useful Options

```bash
python3 render_distance_map.py --help
```

- `--language id` for Indonesian Google address results.
- `--maptype satellite` for satellite imagery.
- `--maptype hybrid` for satellite imagery with road labels.
- `--route driving` to follow roads using Google Directions API.
- `--route straight` to draw a direct line and use Haversine distance.
- `--size 457x640` keeps the 5:7 aspect ratio.
- `--scale 2` renders higher-resolution output.

## Notes

- Photos from WhatsApp, Instagram, or other social apps often lose EXIF/GPS metadata.
- HEIC/HEIF preview/rendering is handled through ImageMagick conversion.
- `--route driving` uses Google Directions API road distance; `--route straight` uses Haversine straight-line distance.
- Generated images and local photos are ignored by git to avoid committing private field data.

## Files

- `generate_location_overlay.py` - render a location caption overlay on a photo.
- `render_distance_map.py` - render a map report between two coordinates/photos.
- `.gitignore` - excludes secrets, generated output, caches, and private media files.
