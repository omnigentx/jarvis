#!/usr/bin/env bash
# Build & push the Jarvis backend base image to Docker Hub.
#
# Usage:
#   ./scripts/build-base-image.sh <version>
#   ./scripts/build-base-image.sh 1.0.0
#
# Produces multi-arch (amd64 + arm64) image and pushes both `:<version>` and
# `:latest` tags. Requires `docker login` already done and buildx available.

set -euo pipefail

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
    echo "Usage: $0 <version>   (e.g. 1.0.0)" >&2
    exit 1
fi

IMAGE="omnigentx/jarvis-backend-base"
BUILDER="jarvis-multiarch"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "${SCRIPT_DIR}")"

cd "${BACKEND_DIR}"

# Multi-arch builds need the docker-container driver, not the default "docker"
# driver. Create a dedicated builder on first run; reuse it afterwards.
if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
    echo "Creating buildx builder '${BUILDER}' (docker-container driver)…"
    docker buildx create --name "${BUILDER}" --driver docker-container --bootstrap
fi
docker buildx use "${BUILDER}"

echo "Building ${IMAGE}:${VERSION} (multi-arch: linux/amd64,linux/arm64)…"
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -t "${IMAGE}:${VERSION}" \
    -t "${IMAGE}:latest" \
    -f Dockerfile.base \
    --push \
    .

echo "✅ Pushed ${IMAGE}:${VERSION} and ${IMAGE}:latest"
echo ""
echo "Next: bump BASE_IMAGE in backend/Dockerfile to ${IMAGE}:${VERSION} and commit."
