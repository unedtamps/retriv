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
validate_bucket "$MINIO_MANIFEST_BUCKET"

if [ "$RETRIV_S3_ACCESS_KEY" = "$RETRIV_S3_ARTIFACT_ACCESS_KEY" ] \
  || [ "$RETRIV_S3_ACCESS_KEY" = "$MINIO_MANIFEST_READER_USER" ] \
  || [ "$RETRIV_S3_ACCESS_KEY" = "$MINIO_MANIFEST_WRITER_USER" ] \
  || [ "$RETRIV_S3_ARTIFACT_ACCESS_KEY" = "$MINIO_MANIFEST_READER_USER" ] \
  || [ "$RETRIV_S3_ARTIFACT_ACCESS_KEY" = "$MINIO_MANIFEST_WRITER_USER" ] \
  || [ "$MINIO_MANIFEST_READER_USER" = "$MINIO_MANIFEST_WRITER_USER" ]; then
  echo "MinIO application users must be distinct" >&2
  exit 2
fi

if [ "$RETRIV_ENVIRONMENT" = "production" ]; then
  case "$MINIO_ROOT_PASSWORD $RETRIV_S3_SECRET_KEY $RETRIV_S3_ARTIFACT_SECRET_KEY $MINIO_MANIFEST_READER_PASSWORD $MINIO_MANIFEST_WRITER_PASSWORD" in
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

case "$MIGRATION_MANIFEST_PREFIX" in
  ""|/*|*..*|*\?*|*\**)
    echo "MINIO_MANIFEST_PREFIX must be a safe relative prefix" >&2
    exit 2
    ;;
esac
case "$MIGRATION_MANIFEST_PREFIX" in
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
if [ "$RETRIV_S3_RAW_BUCKET" = "$RETRIV_S3_ARTIFACT_BUCKET" ] \
  || [ "$RETRIV_S3_RAW_BUCKET" = "$MINIO_MANIFEST_BUCKET" ] \
  || [ "$RETRIV_S3_ARTIFACT_BUCKET" = "$MINIO_MANIFEST_BUCKET" ]; then
  echo "MinIO raw, context, and manifest buckets must be distinct" >&2
  exit 2
fi

mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$RETRIV_S3_RAW_BUCKET"
mc anonymous set none "local/$RETRIV_S3_RAW_BUCKET"
mc mb --ignore-existing "local/$RETRIV_S3_ARTIFACT_BUCKET"
mc anonymous set none "local/$RETRIV_S3_ARTIFACT_BUCKET"
mc mb --ignore-existing "local/$MINIO_MANIFEST_BUCKET"
mc anonymous set none "local/$MINIO_MANIFEST_BUCKET"

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
      "Condition": {"StringLike": {"s3:prefix": ["$MIGRATION_MANIFEST_PREFIX*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET/$MIGRATION_MANIFEST_PREFIX*"]
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
      "Condition": {"StringLike": {"s3:prefix": ["$MIGRATION_MANIFEST_PREFIX*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": ["arn:aws:s3:::$MINIO_MANIFEST_BUCKET/$MIGRATION_MANIFEST_PREFIX*"]
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
mc ilm add --prefix "$MIGRATION_MANIFEST_PREFIX" --expire-days "$MINIO_MANIFEST_RETENTION_DAYS" \
  "local/$MINIO_MANIFEST_BUCKET" >/dev/null
