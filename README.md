# EA LIDAR Download

Download LIDAR tiles from the Environment Agency API for a given area of interest.

This script automatically splits your area of interest by OS 5km grid squares and downloads LIDAR tiles for each intersecting grid square.

## Installation

This project uses `uv` for dependency management. Install dependencies with:

```bash
uv sync
```

Dependencies:
- geopandas
- httpx
- shapely
- tqdm

## Workflow

### 1. Prepare Your Area of Interest

You need an area of interest (AOI) file in any format that geopandas can read (shapefile, GeoJSON, GeoPackage, etc.).

### 2. Discover Available Products

Before downloading, check what products are available for your AOI:

```bash
uv run ea-dl.py /path/to/your/aoi.shp --list-products
```

This queries the API and displays all available products with their years and resolutions:

```
Found 12 product types:

lidar_composite_dtm
  Label: LIDAR Composite DTM
  Years: 2022
  Resolutions: 1, 2m

lidar_composite_first_return_dsm
  Label: LIDAR Composite First Return DSM
  Years: 2022
  Resolutions: 1, 2m

national_lidar_programme_dtm
  Label: National LIDAR Programme DTM
  Years: 2018
  Resolutions: 1m
...
```

### 3. Download Products

Download one or more products using the product IDs from the listing:

```bash
# Download a single product (DTM)
uv run ea-dl.py /path/to/your/aoi.shp \
  --products lidar_composite_dtm

# Download multiple products
uv run ea-dl.py /path/to/your/aoi.shp \
  --products lidar_composite_dtm,lidar_composite_first_return_dsm

# Specify output directory, year, and resolution
uv run ea-dl.py /path/to/your/aoi.shp \
  --products lidar_composite_dtm \
  --year 2022 \
  --resolution 1 \
  --output-dir ./downloaded_tiles
```

### 4. Preview Before Downloading

Use `--dry-run` to see what would be downloaded without actually downloading:

```bash
uv run ea-dl.py /path/to/your/aoi.shp \
  --products lidar_composite_dtm \
  --dry-run
```

## Command-Line Options

```
positional arguments:
  aoi                   Path to AOI file (shapefile, GeoJSON, etc.)

options:
  --output-dir, -o      Output directory for downloaded tiles (default: ./tiles)
  --year                Year of LIDAR data (default: 2022)
  --resolution          Resolution in meters (default: 1)
  --products            Comma-separated list of product types
  --list-products       List all available products for the AOI and exit
  --grid                Path to OS 5km grid shapefile (default: ../ea_lidar/shp/OSGB_Grid_5km.shp)
  --dry-run             Print what would be downloaded without downloading
  --verbose, -v         Verbose output
```

## How It Works

1. Reads your AOI file
2. Finds all OS 5km grid squares that intersect with your AOI
3. For each grid square and product combination:
   - Constructs the API URL
   - Downloads the tile
   - Saves it with a descriptive filename: `{tile_name}_{year}_{resolution}m_{product}.tif`
4. Skips tiles that already exist (allowing resumable downloads)

## Example Workflow

```bash
# 1. Check what's available
uv run ea-dl.py my_study_area.shp --list-products

# 2. Preview the download
uv run ea-dl.py my_study_area.shp \
  --products lidar_composite_dtm,national_lidar_programme_dtm \
  --dry-run

# 3. Download the data
uv run ea-dl.py my_study_area.shp \
  --products lidar_composite_dtm,national_lidar_programme_dtm \
  --output-dir ./lidar_tiles \
  --verbose
```

## Output Files

Downloaded files are named following this pattern:
- `ST73NE_2022_1m_lidar_composite_dtm.tif`
- `SU01SE_2022_1m_lidar_composite_first_return_dsm.tif`

Where:
- `ST73NE` = OS grid tile name
- `2022` = Year of data collection
- `1m` = Resolution
- `lidar_composite_dtm` = Product type

## Common Product Types

- `lidar_composite_dtm` - Digital Terrain Model (ground surface)
- `lidar_composite_first_return_dsm` - Digital Surface Model (first return)
- `lidar_composite_last_return_dsm` - Digital Surface Model (last return)
- `lidar_point_cloud` - Raw point cloud data
- `national_lidar_programme_dtm` - National LIDAR Programme DTM
- `national_lidar_programme_dsm` - National LIDAR Programme DSM
- `national_lidar_programme_point_cloud` - National LIDAR Programme point cloud

Use `--list-products` to see exactly what's available for your specific area.
