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

validate_bucket "$MINIO_BUCKET"
validate_bucket "$MINIO_CONTEXT_BUCKET"
validate_bucket "$MINIO_MANIFEST_BUCKET"

if [ "$MINIO_APP_USER" = "$MINIO_INTERNAL_USER" ] \
  || [ "$MINIO_APP_USER" = "$MINIO_MANIFEST_READER_USER" ] \
  || [ "$MINIO_APP_USER" = "$MINIO_MANIFEST_WRITER_USER" ] \
  || [ "$MINIO_INTERNAL_USER" = "$MINIO_MANIFEST_READER_USER" ] \
  || [ "$MINIO_INTERNAL_USER" = "$MINIO_MANIFEST_WRITER_USER" ] \
  || [ "$MINIO_MANIFEST_READER_USER" = "$MINIO_MANIFEST_WRITER_USER" ]; then
  echo "MinIO application users must be distinct" >&2
  exit 2
fi

if [ "$APP_ENVIRONMENT" = "production" ]; then
  case "$MINIO_ROOT_PASSWORD $MINIO_APP_PASSWORD $MINIO_INTERNAL_PASSWORD $MINIO_MANIFEST_READER_PASSWORD $MINIO_MANIFEST_WRITER_PASSWORD" in
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

case "$MINIO_MANIFEST_PREFIX" in
  ""|/*|*..*|*\?*|*\**)
    echo "MINIO_MANIFEST_PREFIX must be a safe relative prefix" >&2
    exit 2
    ;;
esac
case "$MINIO_MANIFEST_PREFIX" in
  */) ;;
  *)
    echo "MINIO_MANIFEST_PREFIX must end with /" >&2
    exit 2
    ;;
esac
case "$MINIO_MANIFEST_RETENTION_DAYS" in
  ''|*[!0-9]*)
    echo "MINIO_MANIFEST_RETENTION_DAYS must be a positive integer" >&2
    exit 2
    ;;
esac
if [ "$MINIO_BUCKET" = "$MINIO_CONTEXT_BUCKET" ] \
  || [ "$MINIO_BUCKET" = "$MINIO_MANIFEST_BUCKET" ] \
  || [ "$MINIO_CONTEXT_BUCKET" = "$MINIO_MANIFEST_BUCKET" ]; then
  echo "MinIO raw, context, and manifest buckets must be distinct" >&2
  exit 2
fi

mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$MINIO_BUCKET"
mc anonymous set none "local/$MINIO_BUCKET"
mc mb --ignore-existing "local/$MINIO_CONTEXT_BUCKET"
mc anonymous set none "local/$MINIO_CONTEXT_BUCKET"
mc mb --ignore-existing "local/$MINIO_MANIFEST_BUCKET"
mc anonymous set none "local/$MINIO_MANIFEST_BUCKET"

printf '%s\n' \
  '{' \
  '  "Version": "2012-10-17",' \
  '  "Statement": [' \
  '    {' \
  '      "Effect": "Allow",' \
      '      "Action": ["s3:GetBucketLocation", "s3:ListBucket"],' \
  "      \"Resource\": [\"arn:aws:s3:::$MINIO_BUCKET\"]" \
  '    },' \
  '    {' \
  '      "Effect": "Allow",' \
  '      "Action": ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"],' \
  "      \"Resource\": [\"arn:aws:s3:::$MINIO_BUCKET/workspaces/*\"]" \
  '    }' \
  '  ]' \
  '}' > /tmp/ingestion-policy.json

cat > /tmp/internal-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::$MINIO_BUCKET",
        "arn:aws:s3:::$MINIO_CONTEXT_BUCKET"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::$MINIO_CONTEXT_BUCKET"],
      "Condition": {"StringLike": {"s3:prefix": ["workspaces/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::$MINIO_BUCKET/workspaces/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::$MINIO_CONTEXT_BUCKET/workspaces/*"]
    }
  ]
}
EOF

cat > /tmp/manifest-reader-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET"],
      "Condition": {"StringLike": {"s3:prefix": ["$MINIO_MANIFEST_PREFIX*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET/$MINIO_MANIFEST_PREFIX*"]
    }
  ]
}
EOF

cat > /tmp/manifest-writer-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET"],
      "Condition": {"StringLike": {"s3:prefix": ["$MINIO_MANIFEST_PREFIX*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET/$MINIO_MANIFEST_PREFIX*"]
    }
  ]
}
EOF

if ! mc admin user info local "$MINIO_APP_USER" >/dev/null 2>&1; then
  mc admin user add local "$MINIO_APP_USER" "$MINIO_APP_PASSWORD"
fi

mc admin policy create local ingestion-api /tmp/ingestion-policy.json >/dev/null
mc admin policy attach local ingestion-api --user "$MINIO_APP_USER" >/dev/null

if ! mc admin user info local "$MINIO_INTERNAL_USER" >/dev/null 2>&1; then
  mc admin user add local "$MINIO_INTERNAL_USER" "$MINIO_INTERNAL_PASSWORD"
fi
mc admin policy create local internal-context-service /tmp/internal-policy.json >/dev/null
mc admin policy attach local internal-context-service --user "$MINIO_INTERNAL_USER" >/dev/null

if ! mc admin user info local "$MINIO_MANIFEST_READER_USER" >/dev/null 2>&1; then
  mc admin user add local "$MINIO_MANIFEST_READER_USER" "$MINIO_MANIFEST_READER_PASSWORD"
fi
mc admin policy create local migration-manifest-reader /tmp/manifest-reader-policy.json >/dev/null
mc admin policy attach local migration-manifest-reader --user "$MINIO_MANIFEST_READER_USER" >/dev/null

if ! mc admin user info local "$MINIO_MANIFEST_WRITER_USER" >/dev/null 2>&1; then
  mc admin user add local "$MINIO_MANIFEST_WRITER_USER" "$MINIO_MANIFEST_WRITER_PASSWORD"
fi
mc admin policy create local migration-manifest-writer /tmp/manifest-writer-policy.json >/dev/null
mc admin policy attach local migration-manifest-writer --user "$MINIO_MANIFEST_WRITER_USER" >/dev/null
mc ilm rm --all --force "local/$MINIO_MANIFEST_BUCKET" >/dev/null 2>&1 || true
mc ilm add --prefix "$MINIO_MANIFEST_PREFIX" --expire-days "$MINIO_MANIFEST_RETENTION_DAYS" \
  "local/$MINIO_MANIFEST_BUCKET" >/dev/null
