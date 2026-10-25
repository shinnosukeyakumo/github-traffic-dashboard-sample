#!/usr/bin/env bash
# ダッシュボードの Basic 認証の資格情報を CloudFront KeyValueStore に書き込む。
# BASIC_AUTH_PASSWORD を渡さなければランダムに生成し、SSM Parameter Store にも SecureString で控える。
# 使い方: [BASIC_AUTH_USER=...] [BASIC_AUTH_PASSWORD=...] ./scripts/set-basic-auth.sh
set -euo pipefail

STACK_NAME="${STACK_NAME:-GithubTrafficStack}"
BASIC_AUTH_USER="${BASIC_AUTH_USER:-admin}"
PASSWORD_PARAMETER_NAME="${PASSWORD_PARAMETER_NAME:-/github-traffic/basic-auth-password}"

if [ -z "${BASIC_AUTH_PASSWORD:-}" ]; then
  BASIC_AUTH_PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 24)
  aws ssm put-parameter --name "$PASSWORD_PARAMETER_NAME" --type SecureString \
    --value "$BASIC_AUTH_PASSWORD" --overwrite --query Version --output text >/dev/null
  echo "generated password saved to SSM: $PASSWORD_PARAMETER_NAME"
fi

KVS_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='AuthStoreArn'].OutputValue" --output text)
ETAG=$(aws cloudfront-keyvaluestore describe-key-value-store --kvs-arn "$KVS_ARN" --query ETag --output text)
VALUE=$(printf '%s:%s' "$BASIC_AUTH_USER" "$BASIC_AUTH_PASSWORD" | base64 | tr -d '\n')

aws cloudfront-keyvaluestore put-key \
  --kvs-arn "$KVS_ARN" --if-match "$ETAG" \
  --key basic-auth --value "$VALUE" \
  --query ItemCount --output text >/dev/null

echo "saved: basic-auth (user: $BASIC_AUTH_USER) -> $KVS_ARN"
