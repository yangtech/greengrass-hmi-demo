#!/usr/bin/env bash
# provision.sh — Set up an EC2 instance with Greengrass v2.
#
# Run this on a fresh Amazon Linux 2023 or Ubuntu 22.04 EC2 instance.
# Prerequisites:
#   - Instance has an IAM role attached with permissions to provision Greengrass
#   - Security group allows SSH (port 22) from your IP
#   - Bedrock model access enabled in the target region
#
# Usage:
#   bash provision.sh [--region us-east-1] [--thing-name MyGreengrassCore]

set -euo pipefail

# Defaults
REGION="${AWS_REGION:-us-east-1}"
THING_NAME="GreengrassBedrock-Demo"
THING_GROUP="GreengrassBedrock-DemoGroup"
GG_VERSION="2.14.0"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --region) REGION="$2"; shift 2 ;;
        --thing-name) THING_NAME="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=============================================="
echo " Greengrass v2 Provisioning"
echo "  Region:     ${REGION}"
echo "  Thing Name: ${THING_NAME}"
echo "=============================================="
echo ""

# --- Step 1: Install system dependencies ---
echo "[1/4] Installing system dependencies..."

if command -v dnf &>/dev/null; then
    # Amazon Linux 2023
    sudo dnf install -y java-11-amazon-corretto-headless python3 python3-pip unzip curl
elif command -v apt-get &>/dev/null; then
    # Ubuntu
    sudo apt-get update -y
    sudo apt-get install -y default-jdk python3 python3-pip unzip curl
else
    echo "ERROR: Unsupported package manager. Install Java 11+, Python 3.9+, pip manually."
    exit 1
fi

echo "  Java version: $(java -version 2>&1 | head -1)"
echo "  Python version: $(python3 --version)"
echo ""

# --- Step 2: Create Greengrass system user ---
echo "[2/4] Creating Greengrass system user and group..."
sudo useradd --system --create-home ggc_user 2>/dev/null || true
sudo groupadd --system ggc_group 2>/dev/null || true
echo ""

# --- Step 3: Download and install Greengrass v2 ---
echo "[3/4] Downloading and installing Greengrass v2 (${GG_VERSION})..."

GG_INSTALLER_URL="https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-${GG_VERSION}.zip"
INSTALL_DIR="/tmp/greengrass-install"
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

curl -sL "${GG_INSTALLER_URL}" -o greengrass.zip
unzip -qo greengrass.zip -d GreengrassInstaller
echo ""

# --- Step 4: Run the installer with auto-provisioning ---
echo "[4/4] Running Greengrass installer (auto-provision)..."
echo "  This creates: IoT Thing, certificates, Token Exchange Role/Alias"
echo ""

sudo java -Droot="/greengrass/v2" -Dlog.store=FILE \
    -jar "${INSTALL_DIR}/GreengrassInstaller/lib/Greengrass.jar" \
    --aws-region "${REGION}" \
    --thing-name "${THING_NAME}" \
    --thing-group-name "${THING_GROUP}" \
    --component-default-user ggc_user:ggc_group \
    --provision true \
    --setup-system-service true \
    --deploy-dev-tools true

echo ""
echo "=============================================="
echo " Greengrass v2 installed and running!"
echo "=============================================="
echo ""
echo "Verify:"
echo "  sudo systemctl status greengrass"
echo "  sudo /greengrass/v2/bin/greengrass-cli component list"
echo ""
echo "IMPORTANT: Next steps before deploying components:"
echo ""
echo "1. Attach the Bedrock policy to the Token Exchange Role."
echo "   See bedrock-tes-policy.json in this project."
echo ""
echo "   aws iam put-role-policy \\"
echo "     --role-name GreengrassV2TokenExchangeRole \\"
echo "     --policy-name BedrockInvokeAccess \\"
echo "     --policy-document file://bedrock-tes-policy.json \\"
echo "     --region ${REGION}"
echo ""
echo "2. Enable Bedrock model access in the AWS console:"
echo "   https://console.aws.amazon.com/bedrock/home?region=${REGION}#/modelaccess"
echo ""
echo "3. Deploy the demo components:"
echo "   sudo bash deploy.sh"
echo ""
