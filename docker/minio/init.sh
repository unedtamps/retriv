#!/bin/sh
set -eu

validate_bucket() {
  case "$1" in
    ""|*[!a-z0-9.-]*)
      echo "Invalid bucket name: $1" >&2
      exit 2
      ;;
  esac
}

validate_bucket "$RETRIV_S3_RAW_BUCKET"
validate_bucket "$RETRIV_S3_ARTIFACT_BUCKET"

if [ "$RETRIV_S3_ACCESS_KEY" = "$RETRIV_S3_ARTIFACT_ACCESS_KEY" ]; then
  echo "MinIO application users must be distinct" >&2
  exit 2
fi

if [ "$RETRIV_ENVIRONMENT" = "production" ]; then
  case "$MINIO_ROOT_PASSWORD $RETRIV_S3_SECRET_KEY $RETRIV_S3_ARTIFACT_SECRET_KEY" in
    *change-this*|*replace-me*|*development*)
      echo "Production MinIO credentials must not use development placeholders" >&2
      exit 2
      ;;
  esac
  case "$MINIO_ENDPOINT_URL" in
    https://*) ;;
    *)
      echo "Production MinIO endpoint must use HTTPS" >&2
      exit 2
      ;;
  esac
fi

if [ "$RETRIV_S3_RAW_BUCKET" = "$RETRIV_S3_ARTIFACT_BUCKET" ]; then
  echo "MinIO raw and context buckets must be distinct" >&2
  exit 2
fi

mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$RETRIV_S3_RAW_BUCKET"
mc anonymous set none "local/$RETRIV_S3_RAW_BUCKET"
mc mb --ignore-existing "local/$RETRIV_S3_ARTIFACT_BUCKET"
mc anonymous set none "local/$RETRIV_S3_ARTIFACT_BUCKET"

printf '%s\n' \
  '{' \
  '  "Version": "2012-10-17",' \
  '  "Statement": [' \
  '    {' \
  '      "Effect": "Allow",' \
      '      "Action": ["s3:GetBucketLocation", "s3:ListBucket"],' \
  "      \"Resource\": [\"arn:aws:s3:::$RETRIV_S3_RAW_BUCKET\"]" \
  '    },' \
  '    {' \
  '      "Effect": "Allow",' \
  '      "Action": ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"],' \
  "      \"Resource\": [\"arn:aws:s3:::$RETRIV_S3_RAW_BUCKET/workspaces/*\"]" \
  '    }' \
  '  ]' \
  '}' > /tmp/retriv-api-policy.json

cat > /tmp/retriv-worker-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::$RETRIV_S3_RAW_BUCKET",
        "arn:aws:s3:::$RETRIV_S3_ARTIFACT_BUCKET"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::$RETRIV_S3_ARTIFACT_BUCKET"],
      "Condition": {"StringLike": {"s3:prefix": ["workspaces/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::$RETRIV_S3_RAW_BUCKET/workspaces/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::$RETRIV_S3_ARTIFACT_BUCKET/workspaces/*"]
    }
  ]
}
EOF

if ! mc admin user info local "$RETRIV_S3_ACCESS_KEY" >/dev/null 2>&1; then
  mc admin user add local "$RETRIV_S3_ACCESS_KEY" "$RETRIV_S3_SECRET_KEY"
fi

mc admin policy create local retriv-api /tmp/retriv-api-policy.json >/dev/null
mc admin policy attach local retriv-api --user "$RETRIV_S3_ACCESS_KEY" >/dev/null

if ! mc admin user info local "$RETRIV_S3_ARTIFACT_ACCESS_KEY" >/dev/null 2>&1; then
  mc admin user add local "$RETRIV_S3_ARTIFACT_ACCESS_KEY" "$RETRIV_S3_ARTIFACT_SECRET_KEY"
fi
mc admin policy create local retriv-worker /tmp/retriv-worker-policy.json >/dev/null
mc admin policy attach local retriv-worker --user "$RETRIV_S3_ARTIFACT_ACCESS_KEY" >/dev/null
