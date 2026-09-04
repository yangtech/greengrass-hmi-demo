#!/usr/bin/env bash
# deploy.sh — Deploy both components locally using the Greengrass CLI.
#
# Usage:
#   sudo bash deploy.sh
#
# This uses greengrass-cli to create a local deployment, pointing to the
# recipe and artifact directories in this project. No S3 upload needed
# for local development.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPONENTS_DIR="${SCRIPT_DIR}/components"

# Greengrass CLI path (adjust if installed elsewhere)
GG_CLI="${GG_CLI:-/greengrass/v2/bin/greengrass-cli}"

if [[ ! -x "${GG_CLI}" ]]; then
    echo "ERROR: greengrass-cli not found at ${GG_CLI}"
    echo "  Set GG_CLI environment variable to the correct path."
    exit 1
fi

echo "=== Deploying Greengrass components locally ==="
echo "  Components dir: ${COMPONENTS_DIR}"
echo "  Greengrass CLI: ${GG_CLI}"
echo ""

# greengrass-cli expects:
#   --recipeDir : a single directory containing recipe files (*.yaml)
#   --artifactDir : a directory structured as <component>/<version>/
#
# Set up the expected directory layout in a temp deploy dir.
DEPLOY_DIR="${SCRIPT_DIR}/.deploy-staging"
rm -rf "${DEPLOY_DIR}"
mkdir -p "${DEPLOY_DIR}/recipes"
mkdir -p "${DEPLOY_DIR}/artifacts/com.demo.WebApp/1.4.1"
mkdir -p "${DEPLOY_DIR}/artifacts/com.demo.WordCounter/1.0.0"

# Copy recipes (renamed to include component name for clarity)
cp "${COMPONENTS_DIR}/com.demo.WebApp/recipe.yaml" \
   "${DEPLOY_DIR}/recipes/com.demo.WebApp-1.4.1.yaml"
cp "${COMPONENTS_DIR}/com.demo.WordCounter/recipe.yaml" \
   "${DEPLOY_DIR}/recipes/com.demo.WordCounter-1.0.0.yaml"

# Copy artifacts into the expected structure
cp -r "${COMPONENTS_DIR}/com.demo.WebApp/artifacts/"* \
   "${DEPLOY_DIR}/artifacts/com.demo.WebApp/1.4.1/"
cp -r "${COMPONENTS_DIR}/com.demo.WordCounter/artifacts/"* \
   "${DEPLOY_DIR}/artifacts/com.demo.WordCounter/1.0.0/"

# Deploy both components together
"${GG_CLI}" deployment create \
    --recipeDir "${DEPLOY_DIR}/recipes" \
    --artifactDir "${DEPLOY_DIR}/artifacts" \
    --merge "com.demo.WebApp=1.4.1" \
    --merge "com.demo.WordCounter=1.0.0"

echo ""
echo "=== Deployment submitted ==="
echo ""
echo "Check status with:"
echo "  sudo ${GG_CLI} component list"
echo ""
echo "View logs:"
echo "  sudo tail -f /greengrass/v2/logs/com.demo.WebApp.log"
echo "  sudo tail -f /greengrass/v2/logs/com.demo.WordCounter.log"
echo ""
echo "Once running, open: http://localhost:8080"
