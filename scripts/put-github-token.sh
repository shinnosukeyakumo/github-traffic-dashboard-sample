#!/usr/bin/env bash
# GitHub の fine-grained PAT を SSM Parameter Store に SecureString で保存する。
# 使い方: GITHUB_TRAFFIC_TOKEN=... ./scripts/put-github-token.sh
set -euo pipefail

: "${GITHUB_TRAFFIC_TOKEN:?GITHUB_TRAFFIC_TOKEN を設定してください}"
PARAMETER_NAME="${TOKEN_PARAMETER_NAME:-/github-traffic/token}"

aws ssm put-parameter \
  --name "$PARAMETER_NAME" \
  --type SecureString \
  --value "$GITHUB_TRAFFIC_TOKEN" \
  --overwrite \
  --query Version --output text >/dev/null

echo "saved: $PARAMETER_NAME"
