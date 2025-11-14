#!/bin/bash
set -e
set -x

# Log everything to a file for debugging
exec > >(tee -a /var/log/ea-lidar-bootstrap.log)
exec 2>&1

echo "Starting EA LIDAR download bootstrap at $(date)"

# Update and install system dependencies
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    git \
    python3-pip \
    python3-venv \
    awscli \
    unzip \
    gdal-bin \
    libgdal-dev \
    python3-gdal

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"

# Create working directory
WORK_DIR="/opt/ea-lidar"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# Clone the repository
REPO_URL="{{REPO_URL}}"
echo "Cloning repository: $REPO_URL"
git clone "$REPO_URL" .

# Install Python dependencies
echo "Installing Python dependencies with uv"
uv sync

# Locate OS grid shapefile in repo
GRID_PATH="osgb_grid_5km/OSGB_Grid_5km.shp"
if [ ! -f "$GRID_PATH" ]; then
    # Try alternative path
    GRID_PATH="shp/OSGB_Grid_5km.shp"
    if [ ! -f "$GRID_PATH" ]; then
        echo "ERROR: OS grid shapefile not found at osgb_grid_5km/OSGB_Grid_5km.shp or shp/OSGB_Grid_5km.shp"
        exit 1
    fi
fi
echo "Using OS grid shapefile at: $GRID_PATH"

# Handle AOI file
AOI_PATH="{{AOI_PATH}}"
AOI_LOCAL="/tmp/aoi.shp"

if [[ "$AOI_PATH" == s3://* ]]; then
    echo "Downloading AOI from S3: $AOI_PATH"
    # Download all shapefile components
    aws s3 cp "$AOI_PATH" "$AOI_LOCAL"
    BASE_PATH="${AOI_PATH%.shp}"
    LOCAL_BASE="${AOI_LOCAL%.shp}"
    for ext in shx dbf prj cpg; do
        aws s3 cp "${BASE_PATH}.${ext}" "${LOCAL_BASE}.${ext}" 2>/dev/null || true
    done
    AOI_TO_USE="$AOI_LOCAL"
else
    # AOI will be copied via SCP, use the path directly
    AOI_TO_USE="{{AOI_PATH}}"
fi

# Create output directory
OUTPUT_DIR="/opt/ea-lidar/tiles"
mkdir -p "$OUTPUT_DIR"

# Run the download
PRODUCTS="{{PRODUCTS}}"
YEAR="{{YEAR}}"
RESOLUTION="{{RESOLUTION}}"
S3_OUTPUT="{{S3_OUTPUT}}"

echo "Running ea-dl.py with:"
echo "  AOI: $AOI_TO_USE"
echo "  Products: $PRODUCTS"
echo "  Year: $YEAR"
echo "  Resolution: $RESOLUTION"
echo "  S3 Output: $S3_OUTPUT"

uv run ea-dl.py "$AOI_TO_USE" \
    --products "$PRODUCTS" \
    --year "$YEAR" \
    --resolution "$RESOLUTION" \
    --output-dir "$OUTPUT_DIR" \
    --grid "$GRID_PATH" \
    --extract \
    --verbose

# Sync to S3
echo "Syncing files to S3: $S3_OUTPUT"
aws s3 sync "$OUTPUT_DIR" "$S3_OUTPUT"

# Write success marker
echo "SUCCESS" > /tmp/ea-lidar-status
echo "Bootstrap completed successfully at $(date)"

# Signal completion - could use CloudWatch, SNS, or just the status file
# For now, the Python script will poll for the status file via SSH
