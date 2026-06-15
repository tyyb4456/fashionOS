#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# FashionOS — Deploy MCP Servers to Cloud Run
# Run this AFTER cloudbuild.yaml has deployed the main app,
# OR whenever MCP server code changes.
#
# Usage:
#   export GOOGLE_CLOUD_PROJECT=your-project-id
#   chmod +x scripts/deploy-mcps.sh
#   ./scripts/deploy-mcps.sh
#
# After running, note each service URL and update fashionos-worker env vars:
#   gcloud run services update fashionos-worker --region=asia-south1 \
#     --update-env-vars \
#       SHOPIFY_MCP_URL=<shopify-mcp-url>/mcp,\
#       SOCIAL_MCP_URL=<social-mcp-url>/mcp,\
#       TRENDS_MCP_URL=<trends-mcp-url>/mcp,\
#       ADS_MCP_URL=<ads-mcp-url>/mcp
# ─────────────────────────────────────────────────────────────────────────────

set -e

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT first}"
REGION="asia-south1"
REGISTRY="asia-south1-docker.pkg.dev/$PROJECT_ID/fashionos"

echo "Deploying FashionOS MCP servers to Cloud Run ($REGION)..."
echo "Project: $PROJECT_ID"
echo ""

deploy_mcp() {
  local NAME=$1
  local DIR=$2
  local PORT=$3
  local SECRET_FLAGS=$4

  IMAGE="$REGISTRY/$NAME:latest"

  echo "── $NAME ──────────────────────────────────────────────"
  echo "  Building $DIR..."
  docker build -t "$IMAGE" "$DIR/"
  docker push "$IMAGE"

  echo "  Deploying to Cloud Run..."
  # shellcheck disable=SC2086
  gcloud run deploy "$NAME" \
    --image="$IMAGE" \
    --region="$REGION" \
    --platform=managed \
    --port="$PORT" \
    --no-allow-unauthenticated \
    --min-instances=1 \
    --max-instances=1 \
    --memory=512Mi \
    --cpu=1 \
    --timeout=120 \
    $SECRET_FLAGS

  URL=$(gcloud run services describe "$NAME" \
    --region="$REGION" \
    --format="value(status.url)")
  echo "  ✓ $NAME deployed: $URL"
  echo ""
}

# shopify-mcp (port 8001)
deploy_mcp "fashionos-shopify-mcp" \
  "mcp_servers/shopify_mcp" \
  "8001" \
  "--update-secrets=SHOPIFY_SHOP_NAME=fashionos-shopify-shop:latest,SHOPIFY_ACCESS_TOKEN=fashionos-shopify-token:latest"

# social-mcp (port 8002)
deploy_mcp "fashionos-social-mcp" \
  "mcp_servers/social_mcp" \
  "8002" \
  "--update-secrets=APIFY_API_TOKEN=fashionos-apify-token:latest,INSTAGRAM_ACCESS_TOKEN=fashionos-instagram-token:latest,INSTAGRAM_PAGE_ID=fashionos-instagram-page:latest"

# trends-mcp (port 8003)
deploy_mcp "fashionos-trends-mcp" \
  "mcp_servers/trends_mcp" \
  "8003" \
  "--set-env-vars=TRENDS_DEFAULT_GEO=PK"

# ads-mcp (port 8004)
deploy_mcp "fashionos-ads-mcp" \
  "mcp_servers/ads_mcp" \
  "8004" \
  "--update-secrets=META_ACCESS_TOKEN=fashionos-meta-token:latest,META_AD_ACCOUNT_ID=fashionos-meta-account:latest"

# ── Print final URLs for copy-paste ──────────────────────────────────────────
echo "════════════════════════════════════════════════════════"
echo "All MCP servers deployed. Now update fashionos-worker:"
echo ""
echo "gcloud run services update fashionos-worker \\"
echo "  --region=$REGION \\"
echo "  --update-env-vars \\"

for SVC in fashionos-shopify-mcp fashionos-social-mcp fashionos-trends-mcp fashionos-ads-mcp; do
  URL=$(gcloud run services describe "$SVC" \
    --region="$REGION" \
    --format="value(status.url)" 2>/dev/null || echo "unknown")
  case "$SVC" in
    *shopify*) echo "    SHOPIFY_MCP_URL=$URL/mcp,\\" ;;
    *social*)  echo "    SOCIAL_MCP_URL=$URL/mcp,\\" ;;
    *trends*)  echo "    TRENDS_MCP_URL=$URL/mcp,\\" ;;
    *ads*)     echo "    ADS_MCP_URL=$URL/mcp" ;;
  esac
done
echo "════════════════════════════════════════════════════════"