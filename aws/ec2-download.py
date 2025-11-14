#!/usr/bin/env python3
"""
EC2 automation script for downloading EA LIDAR data to S3.

This script launches an EC2 instance, runs the ea-dl.py tool, and syncs
the results to S3. The instance is automatically terminated on success,
but kept running on error for debugging.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import boto3
import paramiko
from botocore.exceptions import ClientError, WaiterError


def get_ubuntu_ami(ec2_client, region: str) -> str:
    """Get the latest Ubuntu 22.04 AMI ID for the region."""
    response = ec2_client.describe_images(
        Owners=["099720109477"],  # Canonical
        Filters=[
            {
                "Name": "name",
                "Values": [
                    "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
                ],
            },
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    )

    if not response["Images"]:
        raise ValueError(f"No Ubuntu 22.04 AMI found in {region}")

    # Sort by creation date and get the latest
    images = sorted(
        response["Images"], key=lambda x: x["CreationDate"], reverse=True
    )
    return images[0]["ImageId"]


def create_security_group(ec2_client, vpc_id: Optional[str] = None) -> str:
    """Create a security group allowing SSH access."""
    try:
        kwargs = {
            "GroupName": f"ea-lidar-sg-{int(time.time())}",
            "Description": "Security group for EA LIDAR download EC2 instance",
        }
        if vpc_id:
            kwargs["VpcId"] = vpc_id

        response = ec2_client.create_security_group(**kwargs)
        sg_id = response["GroupId"]

        # Allow SSH from anywhere (restrict this in production)
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )

        return sg_id
    except ClientError as e:
        raise RuntimeError(f"Failed to create security group: {e}")


def create_iam_role(iam_client, s3_bucket: str) -> str:
    """Create IAM role with S3 write permissions."""
    role_name = f"ea-lidar-role-{int(time.time())}"

    # Trust policy for EC2
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    # Create role
    try:
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for EA LIDAR EC2 instance to write to S3",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise

    # Create inline policy for S3 access
    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:PutObjectAcl",
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                "Resource": [
                    f"arn:aws:s3:::{s3_bucket}/*",
                    f"arn:aws:s3:::{s3_bucket}",
                ],
            }
        ],
    }

    iam_client.put_role_policy(
        RoleName=role_name, PolicyName="S3Access", PolicyDocument=json.dumps(s3_policy)
    )

    # Create instance profile
    profile_name = role_name
    try:
        iam_client.create_instance_profile(InstanceProfileName=profile_name)
        iam_client.add_role_to_instance_profile(
            InstanceProfileName=profile_name, RoleName=role_name
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise

    # Wait for instance profile to be ready
    time.sleep(10)

    return profile_name


def ensure_key_pair(
    ec2_client, key_name: str, ssh_key_path: str, region: str
) -> str:
    """
    Ensure SSH key pair exists in AWS. If not, create it and save locally.
    Returns the path to the SSH key file.
    """
    # Expand tilde in path
    expanded_path = Path(ssh_key_path).expanduser()

    # Check if key pair exists in AWS
    try:
        ec2_client.describe_key_pairs(KeyNames=[key_name])
        print(f"Key pair '{key_name}' already exists in AWS")

        # Check if local key file exists
        if expanded_path.exists():
            print(f"Using existing SSH key at {expanded_path}")
            return str(expanded_path)
        raise ValueError(
            f"Key pair '{key_name}' exists in AWS but local key file "
            f"not found at {expanded_path}. Please provide the correct "
            f"path or delete the key pair from AWS to create a new one."
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
            raise

        # Key pair doesn't exist, create it
        print(f"Creating new key pair '{key_name}' in {region}...")
        response = ec2_client.create_key_pair(KeyName=key_name)

        # Save private key to file
        key_material = response["KeyMaterial"]

        # Create directory if it doesn't exist
        expanded_path.parent.mkdir(parents=True, exist_ok=True)

        # Write key file with secure permissions
        expanded_path.write_text(key_material)
        expanded_path.chmod(0o600)

        print(f"Created and saved new SSH key to {expanded_path}")
        return str(expanded_path)


def prepare_user_data(
    repo_url: str,
    aoi_path: str,
    products: str,
    year: str,
    resolution: str,
    s3_output: str,
) -> str:
    """Prepare user data script with template substitution."""
    script_path = Path(__file__).parent / "bootstrap.sh"
    with open(script_path) as f:
        template = f.read()

    # Replace template variables
    user_data = template.replace("{{REPO_URL}}", repo_url)
    user_data = user_data.replace("{{AOI_PATH}}", aoi_path)
    user_data = user_data.replace("{{PRODUCTS}}", products)
    user_data = user_data.replace("{{YEAR}}", year)
    user_data = user_data.replace("{{RESOLUTION}}", resolution)
    return user_data.replace("{{S3_OUTPUT}}", s3_output)


def launch_instance(
    ec2_client,
    ami_id: str,
    instance_type: str,
    key_name: str,
    security_group_id: str,
    instance_profile: str,
    user_data: str,
    root_volume_size: int = 30,
) -> str:
    """Launch EC2 instance with specified configuration."""
    try:
        response = ec2_client.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            SecurityGroupIds=[security_group_id],
            IamInstanceProfile={"Name": instance_profile},
            UserData=user_data,
            MinCount=1,
            MaxCount=1,
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": root_volume_size,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": "ea-lidar-download"},
                        {"Key": "Project", "Value": "ea-lidar"},
                    ],
                }
            ],
        )

        return response["Instances"][0]["InstanceId"]
    except ClientError as e:
        raise RuntimeError(f"Failed to launch instance: {e}")


def wait_for_instance(ec2_client, instance_id: str) -> str:
    """Wait for instance to be running and return public IP."""
    print(f"Waiting for instance {instance_id} to be running...")

    try:
        waiter = ec2_client.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])
    except WaiterError as e:
        raise RuntimeError(f"Instance failed to start: {e}")

    # Get public IP
    response = ec2_client.describe_instances(InstanceIds=[instance_id])
    instance = response["Reservations"][0]["Instances"][0]
    public_ip = instance.get("PublicIpAddress")

    if not public_ip:
        raise RuntimeError("Instance has no public IP address")

    print(f"Instance running at {public_ip}")
    return public_ip


def upload_aoi_files(
    hostname: str,
    key_path: str,
    local_aoi_path: str,
    remote_path: str = "/tmp/aoi.shp",
) -> None:
    """Upload AOI shapefile and related files via SCP."""
    print(f"Uploading AOI files to {hostname}...")

    # Get all related files (.shp, .shx, .dbf, .prj, etc.)
    base_path = Path(local_aoi_path).with_suffix("")
    files_to_upload = []

    for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx"]:
        file_path = base_path.with_suffix(ext)
        if file_path.exists():
            files_to_upload.append(file_path)

    if not files_to_upload:
        raise ValueError(f"No shapefile components found at {local_aoi_path}")

    # Upload via SFTP
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    max_retries = 10
    for attempt in range(max_retries):
        try:
            ssh.connect(
                hostname, username="ubuntu", key_filename=key_path, timeout=30
            )
            break
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"Connection attempt {attempt + 1} failed, retrying...")
                time.sleep(10)
            else:
                raise RuntimeError(
                    f"Failed to connect after {max_retries} attempts: {e}"
                )

    sftp = ssh.open_sftp()

    remote_base = Path(remote_path).with_suffix("")
    for local_file in files_to_upload:
        remote_file = str(remote_base.with_suffix(local_file.suffix))
        print(f"  Uploading {local_file.name} -> {remote_file}")
        sftp.put(str(local_file), remote_file)

    sftp.close()
    ssh.close()

    print(f"Uploaded {len(files_to_upload)} files")


def monitor_job(
    hostname: str,
    key_path: str,
    log_file: str = "/var/log/ea-lidar-bootstrap.log",
    status_file: str = "/tmp/ea-lidar-status",
) -> bool:
    """Monitor job progress by tailing logs and checking status file."""
    print("Monitoring job progress...")
    print(
        f"\nYou can also SSH to the instance and run: tail -f {log_file}\n"
    )

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname, username="ubuntu", key_filename=key_path)

    # Wait for log file to exist
    print("Waiting for bootstrap to start...")
    for _ in range(30):
        stdin, stdout, stderr = ssh.exec_command(f"test -f {log_file} && echo exists")
        if stdout.read().decode().strip() == "exists":
            break
        time.sleep(2)

    print("\n--- Bootstrap Log ---")
    last_line_count = 0

    # Poll for updates instead of using tail -f
    while True:
        # Check if status file exists
        stdin_check, stdout_check, stderr_check = ssh.exec_command(
            f"cat {status_file} 2>/dev/null"
        )
        status = stdout_check.read().decode().strip()

        if status:
            # Print any remaining log lines
            stdin, stdout, stderr = ssh.exec_command(
                f"tail -n +{last_line_count + 1} {log_file} 2>/dev/null"
            )
            remaining_lines = stdout.read().decode()
            if remaining_lines:
                print(remaining_lines, end="")

            print(f"\n--- Job Status: {status} ---")
            success = status == "SUCCESS"
            ssh.close()
            return success

        # Get new log lines since last check
        stdin, stdout, stderr = ssh.exec_command(
            f"wc -l {log_file} 2>/dev/null | awk '{{print $1}}'"
        )
        try:
            current_line_count = int(stdout.read().decode().strip() or "0")
        except ValueError:
            current_line_count = 0

        if current_line_count > last_line_count:
            # Get new lines
            stdin, stdout, stderr = ssh.exec_command(
                f"tail -n +{last_line_count + 1} {log_file} 2>/dev/null | head -n {current_line_count - last_line_count}"
            )
            new_lines = stdout.read().decode()
            if new_lines:
                print(new_lines, end="")
            last_line_count = current_line_count

        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(
        description="Automate EA LIDAR downloads using EC2"
    )
    parser.add_argument(
        "aoi", help="Path to AOI file (local path or s3:// URI)"
    )
    parser.add_argument(
        "--s3-output",
        required=True,
        help="S3 output location (s3://bucket/prefix)",
    )
    parser.add_argument(
        "--products",
        default="lidar_composite_dtm",
        help="Comma-separated list of products to download",
    )
    parser.add_argument("--year", default="2022", help="Year of LIDAR data")
    parser.add_argument(
        "--resolution", default="1", help="Resolution in meters"
    )
    parser.add_argument(
        "--ssh-key",
        default="~/.ssh/ea-lidar-key.pem",
        help="Path to SSH private key (default: ~/.ssh/ea-lidar-key.pem). "
        "Will be created if it doesn't exist.",
    )
    parser.add_argument(
        "--key-name",
        default="ea-lidar-key",
        help="Name of EC2 key pair (default: ea-lidar-key). "
        "Will be created if it doesn't exist.",
    )
    parser.add_argument("--region", default="eu-west-2", help="AWS region")
    parser.add_argument(
        "--instance-type", default="t3.medium", help="EC2 instance type"
    )
    parser.add_argument(
        "--volume-size", type=int, default=30, help="Root volume size in GB"
    )
    parser.add_argument(
        "--repo-url",
        default="https://github.com/yourusername/ea-lidar-download.git",
        help="Git repository URL",
    )
    parser.add_argument(
        "--no-terminate",
        action="store_true",
        help="Do not terminate instance on success (for debugging)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.s3_output.startswith("s3://"):
        parser.error("--s3-output must start with s3://")

    s3_bucket = args.s3_output.split("/")[2]

    is_local_aoi = not args.aoi.startswith("s3://")
    if is_local_aoi and not Path(args.aoi).exists():
        parser.error(f"AOI file not found: {args.aoi}")

    # Initialize AWS clients
    ec2_client = boto3.client("ec2", region_name=args.region)
    iam_client = boto3.client("iam", region_name=args.region)

    # Ensure SSH key pair exists (create if needed)
    args.ssh_key = ensure_key_pair(
        ec2_client, args.key_name, args.ssh_key, args.region
    )

    instance_id = None
    sg_id = None

    try:
        # Get AMI
        print("Finding Ubuntu AMI...")
        ami_id = get_ubuntu_ami(ec2_client, args.region)
        print(f"Using AMI: {ami_id}")

        # Create security group
        print("Creating security group...")
        sg_id = create_security_group(ec2_client)
        print(f"Created security group: {sg_id}")

        # Create IAM role
        print("Creating IAM role...")
        instance_profile = create_iam_role(iam_client, s3_bucket)
        print(f"Created IAM role: {instance_profile}")

        # Prepare user data
        aoi_path = "/tmp/aoi.shp" if is_local_aoi else args.aoi
        user_data = prepare_user_data(
            repo_url=args.repo_url,
            aoi_path=aoi_path,
            products=args.products,
            year=args.year,
            resolution=args.resolution,
            s3_output=args.s3_output,
        )

        # Launch instance
        print("Launching EC2 instance...")
        instance_id = launch_instance(
            ec2_client,
            ami_id=ami_id,
            instance_type=args.instance_type,
            key_name=args.key_name,
            security_group_id=sg_id,
            instance_profile=instance_profile,
            user_data=user_data,
            root_volume_size=args.volume_size,
        )
        print(f"Launched instance: {instance_id}")

        # Wait for instance
        public_ip = wait_for_instance(ec2_client, instance_id)

        # Upload AOI if local
        if is_local_aoi:
            upload_aoi_files(public_ip, args.ssh_key, args.aoi)

        # Monitor job
        success = monitor_job(public_ip, args.ssh_key)

        if success:
            print("\nJob completed successfully!")
            if not args.no_terminate:
                print(f"Terminating instance {instance_id}...")
                ec2_client.terminate_instances(InstanceIds=[instance_id])
            else:
                print(f"Instance {instance_id} left running (--no-terminate)")
        else:
            print("\nJob failed!")
            print(f"Instance {instance_id} left running for debugging")
            print(f"Connect with: ssh -i {args.ssh_key} ubuntu@{public_ip}")
            sys.exit(1)

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        if instance_id:
            print(f"Instance {instance_id} left running for debugging")
            response = ec2_client.describe_instances(InstanceIds=[instance_id])
            public_ip = response["Reservations"][0]["Instances"][0].get(
                "PublicIpAddress"
            )
            if public_ip:
                print(f"Connect with: ssh -i {args.ssh_key} ubuntu@{public_ip}")
        sys.exit(1)


if __name__ == "__main__":
    main()
