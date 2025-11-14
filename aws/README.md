# AWS EC2 Automation for EA LIDAR Downloads

This directory contains scripts to automate EA LIDAR downloads using EC2 instances that upload results directly to S3.

## Why Use This?

If you have slow local bandwidth or need to download large datasets, running the download on EC2 avoids transferring data through your local machine. The EC2 instance downloads from EA, extracts files, and syncs directly to S3.

## Prerequisites

1. AWS CLI configured with credentials (`aws configure`)
2. S3 bucket for storing results
3. Python dependencies installed in the parent directory:
   ```bash
   cd .. && uv sync
   ```

The script will automatically create an SSH key pair if one doesn't exist.

## Required AWS Permissions

Your AWS credentials need these permissions:

**EC2:**
- `RunInstances`
- `DescribeInstances`
- `TerminateInstances`
- `CreateSecurityGroup`
- `AuthorizeSecurityGroupIngress`
- `DescribeImages`
- `CreateKeyPair`
- `DescribeKeyPairs`

**IAM:**
- `CreateRole`
- `PutRolePolicy`
- `CreateInstanceProfile`
- `AddRoleToInstanceProfile`

**S3:**
- Read/write access to your output bucket

## Basic Usage

Minimal example (SSH key will be auto-created if it doesn't exist):

```bash
cd /path/to/ea-lidar-download
uv run python aws/ec2-download.py /path/to/aoi.shp \
  --s3-output s3://my-bucket/lidar-data/project1 \
  --repo-url https://github.com/username/ea-lidar-download.git
```

The script will automatically create an SSH key pair named `ea-lidar-key` and save it to `~/.ssh/ea-lidar-key.pem` if they don't already exist.

## Required Arguments

- `aoi` - Path to AOI shapefile (local path or `s3://bucket/path/to/aoi.shp`)
- `--s3-output` - S3 destination (e.g., `s3://my-bucket/prefix`)
- `--repo-url` - Git repository URL to clone on the instance

## Optional Arguments

- `--products` - Comma-separated product list (default: `lidar_composite_dtm`)
- `--year` - Year of data (default: `2022`)
- `--resolution` - Resolution in meters (default: `1`)
- `--ssh-key` - Path to SSH private key (default: `~/.ssh/ea-lidar-key.pem`, auto-created if missing)
- `--key-name` - EC2 key pair name (default: `ea-lidar-key`, auto-created if missing)
- `--region` - AWS region (default: `eu-west-2`)
- `--instance-type` - EC2 instance type (default: `t3.medium`)
- `--volume-size` - Root volume size in GB (default: `30`)
- `--no-terminate` - Keep instance running after success (for debugging)

## Example: Download Multiple Products

```bash
uv run python aws/ec2-download.py my_study_area.shp \
  --s3-output s3://my-bucket/cranborne-lidar \
  --products lidar_composite_dtm,lidar_composite_first_return_dsm \
  --year 2022 \
  --resolution 1 \
  --repo-url https://github.com/myuser/ea-lidar-download.git
```

## Example: Using S3 AOI

If your AOI is already in S3:

```bash
uv run python aws/ec2-download.py s3://my-bucket/aois/study-area.shp \
  --s3-output s3://my-bucket/lidar-output \
  --repo-url https://github.com/myuser/ea-lidar-download.git
```

## How It Works

1. **Launch**: Creates EC2 instance with Ubuntu 22.04
2. **Configure**: Creates security group for SSH and IAM role for S3 access
3. **Upload AOI**: If local, uploads shapefile via SFTP; if S3, instance downloads it
4. **Bootstrap**: Instance clones repo, installs dependencies, downloads OS grid
5. **Download**: Runs `ea-dl.py` with `--extract` flag (streaming unzip)
6. **Upload**: Syncs extracted files to S3
7. **Cleanup**: Terminates instance on success, keeps it running on error

## Monitoring Progress

The script tails the bootstrap log in real-time, showing:
- Dependency installation progress
- Download progress for each tile
- Extraction status
- S3 sync progress

## Cost Estimates

- **Instance**: ~$0.04/hour for t3.medium
- **Storage**: ~$0.10/GB/month for EBS (deleted on termination)
- **S3**: Standard pricing for stored data
- **Transfer**: Free from EC2 to S3 in same region

Typical job (50-100 tiles, 1-2 products): **30-60 minutes = $0.02-$0.04**

## Troubleshooting

### Job Failed - Instance Left Running

If the download fails, the instance stays running for debugging:

```bash
# The script prints the SSH command, e.g.:
ssh -i ~/.ssh/my-key.pem ubuntu@<instance-ip>
```

Once connected, check:
- `/var/log/ea-lidar-bootstrap.log` - Full bootstrap log
- `/tmp/ea-lidar-status` - Job status file
- `/opt/ea-lidar/` - Working directory

When done debugging:
```bash
aws ec2 terminate-instances --instance-ids <instance-id>
```

### Instance Won't Start

- Check AWS service quotas (vCPU limits)
- Verify key pair exists in the specified region
- Ensure IAM permissions are correct

### SSH Connection Fails

The script retries SSH connections for up to 100 seconds. If it still fails:
- Check security group allows SSH from your IP
- Verify the key pair name matches the SSH key file
- Try manually: `ssh -i <key> ubuntu@<ip>`

### Files Not Appearing in S3

- Check IAM role has S3 write permissions to the bucket
- Verify S3 bucket name is correct and accessible
- Check `/var/log/ea-lidar-bootstrap.log` for AWS CLI errors

## Files in This Directory

- `ec2-download.py` - Main automation script
- `bootstrap.sh` - User data script that runs on EC2 instance
- `README.md` - This file
