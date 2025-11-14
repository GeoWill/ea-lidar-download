#!/usr/bin/env python3
"""
Download LIDAR tiles from the Environment Agency API for a given area of interest.

This script splits an AOI by OS 5km grid squares and downloads LIDAR tiles
for each grid square that intersects the AOI.
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import geopandas as gp
import httpx
from shapely.geometry import mapping
from tqdm import tqdm


def get_os_grid_tiles(
    aoi_gdf: gp.GeoDataFrame, grid_path: str
) -> gp.GeoDataFrame:
    """
    Find all OS 5km grid tiles that intersect with the AOI.

    Args:
        aoi_gdf: GeoDataFrame containing the area of interest
        grid_path: Path to the OS grid shapefile

    Returns:
        GeoDataFrame of intersecting grid tiles with TILE_NAME column
    """
    # Read OS grid
    os_grid = gp.read_file(grid_path)

    # Ensure both are in same CRS (EPSG:27700 - British National Grid)
    if aoi_gdf.crs != "EPSG:27700":
        aoi_gdf = aoi_gdf.to_crs("EPSG:27700")
    if os_grid.crs != "EPSG:27700":
        os_grid = os_grid.to_crs("EPSG:27700")

    # Use spatial index for efficient intersection
    return os_grid[os_grid.intersects(aoi_gdf.union_all())]


def query_available_products(
    aoi_gdf: gp.GeoDataFrame, use_full_aoi: bool = False
) -> List[dict]:
    """
    Query the API to find all available products for the AOI.

    Args:
        aoi_gdf: GeoDataFrame containing the area of interest
        use_full_aoi: If True, use the full AOI geometry. If False, use a small sample.

    Returns:
        List of product information dictionaries with tile URIs
    """
    # Convert AOI to WGS84 (EPSG:4326) for the API
    aoi_wgs84 = aoi_gdf.to_crs("EPSG:4326")

    if use_full_aoi:
        # Use the full AOI to get all available tiles
        geom = mapping(aoi_wgs84.union_all())
    else:
        # Get a small representative geometry from the AOI for listing products
        centroid = aoi_wgs84.union_all().centroid
        small_poly = centroid.buffer(0.001)  # Small buffer in degrees
        geom = mapping(small_poly)

    url = "https://environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search"

    try:
        with httpx.Client(timeout=30.0) as client:
            headers = {
                "Content-Type": "application/geo+json",
                "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
            }

            response = client.post(url, headers=headers, json=geom)
            response.raise_for_status()

            data = response.json()
            return data.get("results", [])

    except Exception as e:
        print(f"Error querying API: {e}")
        return []


def organize_products(results: List[dict]) -> Dict[str, Dict]:
    """
    Organize API results by product type, showing available years and resolutions.

    Args:
        results: List of product results from the API

    Returns:
        Dictionary organized by product ID with available years and resolutions
    """
    products = defaultdict(
        lambda: {"label": "", "years": set(), "resolutions": set()}
    )

    for result in results:
        product_id = result["product"]["id"]
        product_label = result["product"]["label"]
        year = result["year"]["id"]
        resolution = result["resolution"]["id"]

        products[product_id]["label"] = product_label
        products[product_id]["years"].add(year)
        products[product_id]["resolutions"].add(resolution)

    return products


def download_tile(
    tile_name: str,
    url: str,
    output_dir: Path,
    year: str,
    resolution: str,
    product: str,
    dry_run: bool = False,
) -> bool:
    """
    Download a single LIDAR tile from a given URL.

    Args:
        tile_name: OS grid tile name (e.g., 'ST8520')
        url: Full URL to download the tile from (from API search results)
        output_dir: Directory to save downloaded files
        year: Year of data
        resolution: Resolution in meters
        product: Product type (e.g., 'lidar_composite_dtm')
        dry_run: If True, only print what would be downloaded

    Returns:
        True if successful, False otherwise
    """
    # Add subscription key if not present
    if "subscription-key" not in url:
        url = f"{url}?subscription-key=public"

    # Create directory structure: product/year/resolution/
    product_dir = output_dir / product / year / f"{resolution}m"
    product_dir.mkdir(parents=True, exist_ok=True)

    output_file = product_dir / f"{tile_name}.zip"

    if dry_run:
        print(f"Would download: {url}")
        print(f"         to: {output_file}")
        return True

    if output_file.exists():
        print(f"Skipping {tile_name} - already exists")
        return True

    try:
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://environment.data.gov.uk/",
            }

            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    print(f"Tile {tile_name} not available (404)")
                    return False

                response.raise_for_status()

                total = int(response.headers.get("content-length", 0))

                with (
                    open(output_file, "wb") as f,
                    tqdm(
                        total=total, unit="B", unit_scale=True, desc=tile_name
                    ) as pbar,
                ):
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        pbar.update(len(chunk))

        return True

    except httpx.HTTPStatusError as e:
        print(f"HTTP error downloading {tile_name}: {e}")
        return False
    except Exception as e:
        print(f"Error downloading {tile_name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download LIDAR tiles from Environment Agency API for an area of interest"
    )
    parser.add_argument(
        "aoi", type=str, help="Path to AOI file (shapefile, GeoJSON, etc.)"
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default="./tiles",
        help="Output directory for downloaded tiles (default: ./tiles)",
    )
    parser.add_argument(
        "--year",
        type=str,
        required=True,
        help="Year of LIDAR data (use --list-products to see available years)",
    )
    parser.add_argument(
        "--resolution",
        type=str,
        default="1",
        help="Resolution in meters (default: 1)",
    )
    parser.add_argument(
        "--products",
        type=str,
        help="Comma-separated list of product types (e.g., lidar_composite_dtm,lidar_composite_dsm)",
    )
    parser.add_argument(
        "--list-products",
        action="store_true",
        help="List all available products for the AOI and exit",
    )
    parser.add_argument(
        "--grid",
        type=str,
        default="osgb_grid_5km/OSGB_Grid_5km.shp",
        help="Path to OS 5km grid shapefile",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without downloading",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )

    args = parser.parse_args()

    # Read AOI
    if args.verbose:
        print(f"Reading AOI from {args.aoi}")

    try:
        aoi_gdf = gp.read_file(args.aoi)
    except Exception as e:
        print(f"Error reading AOI file: {e}")
        sys.exit(1)

    # If --list-products, query API and display available products
    if args.list_products:
        print("Querying API for available products...")
        results = query_available_products(aoi_gdf)

        if not results:
            print("No products found or error querying API")
            sys.exit(1)

        products = organize_products(results)

        print(f"\nFound {len(products)} product types:\n")

        for product_id, info in sorted(products.items()):
            print(f"{product_id}")
            print(f"  Label: {info['label']}")
            years = sorted(info["years"], reverse=True)
            print(f"  Years: {', '.join(years)}")
            resolutions = sorted([r for r in info["resolutions"] if r != "NaN"])
            if resolutions:
                print(f"  Resolutions: {'m, '.join(resolutions)}m")
            print()

        sys.exit(0)

    # Parse products list
    if args.products:
        product_list = [p.strip() for p in args.products.split(",")]
    else:
        product_list = ["lidar_composite_dtm"]
        print("No products specified, defaulting to lidar_composite_dtm")

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query API for available tiles
    print("Querying API for available tiles in AOI...")
    results = query_available_products(aoi_gdf, use_full_aoi=True)

    if not results:
        print("No tiles found for this AOI")
        sys.exit(1)

    # Filter results by requested products, year, and resolution
    tiles_to_download = []
    for result in results:
        product_id = result["product"]["id"]
        year_id = result["year"]["id"]
        resolution_id = result["resolution"]["id"]

        if (
            product_id in product_list
            and year_id == args.year
            and resolution_id == args.resolution
        ):
            tiles_to_download.append(
                {
                    "tile_name": result["tile"]["id"],
                    "url": result["uri"],
                    "product": product_id,
                    "year": year_id,
                    "resolution": resolution_id,
                }
            )

    if not tiles_to_download:
        print(
            f"No tiles found matching products={product_list}, year={args.year}, resolution={args.resolution}"
        )
        print("\nUse --list-products to see what is available for this AOI")
        sys.exit(1)

    print(f"Found {len(tiles_to_download)} tiles to download")

    if args.verbose:
        tile_names = sorted({t["tile_name"] for t in tiles_to_download})
        print(f"Tiles: {', '.join(tile_names)}")

    # Download each tile
    successful = 0
    failed = 0

    for tile_info in tiles_to_download:
        success = download_tile(
            tile_info["tile_name"],
            tile_info["url"],
            output_dir,
            tile_info["year"],
            tile_info["resolution"],
            tile_info["product"],
            dry_run=args.dry_run,
        )

        if success:
            successful += 1
        else:
            failed += 1

    print(
        f"\nComplete: {successful}/{len(tiles_to_download)} successful, {failed}/{len(tiles_to_download)} failed"
    )

    if not args.dry_run:
        print(f"Files saved to: {output_dir.absolute()}")


if __name__ == "__main__":
    main()
