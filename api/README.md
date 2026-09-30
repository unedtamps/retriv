# Retriv API

Backend FastAPI untuk menerima raw document perusahaan dengan scope per workspace.
Versi MVP ini menerima `PDF`, `DOCX`, dan `XLSX`; file asli disimpan pada bucket private
MinIO dan metadata disimpan di PostgreSQL.

## Scope MVP

- JWT access token berlaku 60 menit.
- User awal dibuat melalui one-shot provisioning manifest; user baru dibuat oleh platform admin.
- `admin` adalah platform role untuk dashboard dan tidak otomatis memberi akses data workspace.
- Akses workspace disimpan pada tabel membership dengan role `owner`, `editor`, atau `viewer`.
- Semua user terautentikasi dapat melihat katalog workspace, tetapi document data hanya dapat diakses
  melalui membership yang sesuai.
- Workspace dibuat melalui provisioning manifest atau admin dashboard dan setiap document route wajib
  memakai `workspace_id`.
- File yang sama di workspace yang sama dideduplikasi menggunakan SHA-256.
- Batas default upload adalah 50 MiB dan PDF maksimal 500 halaman.
- Password-protected/encrypted document dan macro-enabled Office document ditolak.
- ClamAV memindai upload sebelum file divalidasi dan disimpan ke MinIO.
- OCR, parsing isi, chunking, dan embedding berjalan di service internal. Semantic Q&A berjalan
  melalui `retriv-agent` dan hanya menerima context dari scope workspace yang diverifikasi oleh API.

## Prerequisites

- Docker Engine dan Docker Compose v2.
- MinIO Client (`mc`) untuk upload manifest, atau akses ke MinIO Console.
- `uv` untuk menjalankan test/lint lokal, atau Python 3.12+.
- RAM minimal sekitar 4 GiB untuk ClamAV pada first startup.

## Menjalankan Lokal

Salin konfigurasi contoh dan ganti semua secret placeholder:

```bash
cp .env.example .env
openssl rand -hex 32
```

Gunakan secret acak yang berbeda untuk `APP_JWT_SECRET` dan `OUTBOX_SIGNING_SECRET` di
`.env`, lalu ganti juga
`POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`, `MINIO_APP_PASSWORD`,
`MINIO_INTERNAL_PASSWORD`, `MINIO_MANIFEST_READER_PASSWORD`, dan
`MINIO_MANIFEST_WRITER_PASSWORD`. Kemudian jalankan:

```bash
docker compose up -d
docker compose ps
curl http://127.0.0.1:9000/minio/health/live
```

Compose ini hanya menjalankan infrastructure: PostgreSQL, MinIO, bucket initialization, dan
ClamAV. Jalankan migration dan API dari host pada terminal pertama setelah infrastructure sehat:

```bash
uv run --env-file .env -- alembic upgrade head
uv run --env-file .env -- uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Jalankan dashboard dari project `retriv-web` pada terminal lain. Konfigurasi `.env.example`
backend sudah mengizinkan kedua origin development Vite secara eksplisit:

```bash
cd ../web
npm ci
npm run dev
```

Dashboard tersedia di `http://127.0.0.1:5173/dashboard`.

Untuk chat atas dokumen, siapkan `retriv-agent` dan `retriv-core`, isi variabel
`APP_AGENT_*` pada `.env`, serta set `APP_PROCESSING_API_BASE_URL` dan
`APP_PROCESSING_API_TOKEN` untuk menunjuk ke API internal (token harus sama dengan
`INTERNAL_API_TOKEN` pada `retriv-core/.env`). Buka `http://127.0.0.1:5173/ask-context`.
Notifikasi pemrosesan memuat seluruh workspace yang dapat dibaca user, memperlihatkan proses aktif
terbaru dan aktivitas satu jam terakhir, serta diperbarui setiap 10 detik. Setiap percakapan memiliki
ID dan dimiliki oleh user pembuatnya; pertanyaan, jawaban, serta referensi sumber disimpan di
PostgreSQL utama Retriv. Chunk hasil retrieval dan output tool tidak disimpan terpisah, tetapi
jawaban tersimpan dapat mengutip teks sumber. Untuk mencegah jawaban lama menjadi bukti baru,
jawaban berbasis dokumen tetap terlihat di transcript privat tetapi teks dan referensi sumbernya
tidak dikirim kembali ke model; pertanyaan user, workspace provenance, dan pencarian segar digunakan
untuk melanjutkan chat.
Retriv menghitung membership `search_context` saat tiap pesan dimulai dan menandatangani scope
workspace untuk agent. Pesan salam sederhana dijawab secara statis; pesan lainnya harus memperoleh
bukti dari tool retrieval yang dipilih model sebelum jawaban dapat dikembalikan. Agent memverifikasi
tanda tangan dan menjalankan loop PydanticAI terbatas, dengan retrieval hanya melalui wrapper
server-side. Jika tidak ada bukti relevan yang lolos ambang skor, API mengembalikan jawaban tetap
tanpa memanggil model untuk menjawab dari pengetahuan umum. Perubahan membership berlaku pada pesan
berikutnya; turn lama dari workspace yang sudah dicabut tidak diteruskan sebagai konteks ke model.
Lihat `../agent/README.md` untuk konfigurasi dan setup agent.

Setelah `retriv-core` siap pada port `8082`, jalankan outbox relay dari host pada terminal
kedua:

```bash
uv run --env-file .env -- python -m outbox
```

Receiver untuk menguji outbox adalah service terpisah di luar repo ini. Jalankan receiver tersebut
lalu arahkan `OUTBOX_TARGET_URL` ke address-nya. Compose default tidak menjalankan API atau relay.

Startup pertama ClamAV dapat memerlukan waktu karena database signature diunduh. Port
PostgreSQL (`127.0.0.1:5432`) dan ClamAV (`127.0.0.1:3310`) hanya dibuka pada loopback host
agar aplikasi host dapat terhubung.

Data disimpan pada Docker named volumes:

- `retriv-api_postgres_data`
- `retriv-api_minio_data`
- `retriv-api_clamav_data`

Tidak ada folder data yang di-bind mount ke host. `docker compose down` tidak menghapus
data; `docker compose down -v` menghapus seluruh volume dan seluruh data lokal.

MinIO API dan Console hanya dibuka pada loopback host:

- S3 API: `http://127.0.0.1:9000`
- Console: `http://127.0.0.1:9001`
- PostgreSQL: `127.0.0.1:5432`
- ClamAV: `127.0.0.1:3310`

Initializer MinIO membuat tiga bucket private. User ingestion dapat mengelola object raw di
`raw-documents/workspaces/*`. User `MINIO_INTERNAL_USER` hanya dapat membaca object raw pada
prefix tersebut, tanpa izin write/delete, dan dapat membaca, menulis, menghapus, serta me-list
object pada `context-documents/workspaces/*` untuk prefix-based artifact cleanup. Credential
migration reader dan writer tetap dibatasi ke prefix manifest. Semua username aplikasi MinIO harus
berbeda.

## Provisioning User dan Workspace

Provisioning dijalankan sebagai job satu kali yang tidak memiliki HTTP endpoint atau public
port. Manifest di-upload manual ke bucket private `migration-manifests` menggunakan credential
writer, sedangkan job hanya memiliki akses read-only.

Contoh manifest `users-v1.json`:

```json
{
  "schema_version": 1,
  "migration_id": "bootstrap-users-2026-09-20",
  "users": [
    {
      "username": "admin",
      "password": "ganti-dengan-password-minimal-12-karakter",
      "role": "admin",
      "is_active": true
    }
  ],
  "workspaces": [
    {
      "slug": "finance",
      "name": "Finance",
      "description": "Finance workspace"
    }
  ]
}
```

Password di manifest di-hash Argon2id dalam memory dan tidak ditulis ke log aplikasi.
Karena password berada di object storage sebelum job berjalan, gunakan bucket private dan
credential writer hanya untuk operator provisioning.

Upload manifest dari host melalui MinIO API memakai credential writer:

```bash
set -a; . ./.env; set +a
mc alias set local http://127.0.0.1:9000 \
  "$MINIO_MANIFEST_WRITER_USER" "$MINIO_MANIFEST_WRITER_PASSWORD"
mc cp users-v1.json local/migration-manifests/bootstrap/users-v1.json
```

Set `MIGRATION_MANIFEST_URI` di `.env`, lalu jalankan job:

```bash
set -a; . ./.env; set +a
PROVISIONER_DATABASE_URL="$APP_DATABASE_URL" \
PROVISIONER_S3_ENDPOINT_URL="$APP_S3_ENDPOINT_URL" \
PROVISIONER_S3_ACCESS_KEY="$MINIO_MANIFEST_READER_USER" \
PROVISIONER_S3_SECRET_KEY="$MINIO_MANIFEST_READER_PASSWORD" \
PROVISIONER_MANIFEST_BUCKET="$MINIO_MANIFEST_BUCKET" \
PROVISIONER_RAW_DOCUMENT_BUCKET="$MINIO_BUCKET" \
PROVISIONER_MANIFEST_PREFIX="$MIGRATION_MANIFEST_PREFIX" \
PROVISIONER_ENVIRONMENT="$APP_ENVIRONMENT" \
PROVISIONER_MANIFEST_URI="$MIGRATION_MANIFEST_URI" \
uv run python -m provisioning
```

Job mencetak ringkasan JSON. `migration_id` dan hash payload dicatat di PostgreSQL. Menjalankan
manifest yang sama lagi menjadi no-op; `migration_id` yang sama dengan payload berbeda ditolak.
Username dan workspace slug di-upsert, UUID existing dipertahankan, dan data yang tidak ada di
manifest tidak dihapus.

Provisioning tidak memberikan akses workspace secara implisit. Untuk workspace hasil bootstrap,
platform admin harus menetapkan membership `owner`, `editor`, atau `viewer` melalui endpoint admin.

## Event Outbox

Setelah upload baru berhasil, aplikasi menyimpan metadata document dan event `document.ready`
dalam transaksi PostgreSQL yang sama. Penghapusan mengubah status menjadi `deleting` dan
menyimpan satu event version-1 `document.deleted` dalam transaksi yang sama sebelum menghapus
object S3. Retry memakai event yang sama; metadata document dihapus setelah S3 berhasil. Payload
deletion berisi `document_id`, `workspace_id`, dan `sha256`. Payload event hanya berisi metadata
dan pointer object MinIO; isi file tidak dikirim ke receiver.

Event disimpan di tabel `outbox_events` dengan status delivery:

```text
pending -> processing -> published
                    \-> dead_letter
```

Relay dijalankan sebagai proses host terpisah. Relay mengambil event pending, mengirim HTTP `POST`
ke receiver, memakai `event.id` sebagai `Idempotency-Key`, dan melakukan retry exponential
backoff untuk timeout atau response `5xx`. Setiap claim memakai UUID lease token agar worker
lama tidak dapat menyelesaikan lease yang sudah diambil ulang:

```bash
uv run --env-file .env -- python -m outbox
```

Konfigurasi contoh mengarah ke receiver durable `retriv-core` pada
`http://127.0.0.1:8082/internal/events`. Untuk menguji dengan dummy receiver, jalankan relay dengan
override sementara:

```bash
OUTBOX_TARGET_URL=http://127.0.0.1:8081/events \
  uv run --env-file .env -- python -m outbox
```

Set `OUTBOX_ENVIRONMENT=production` pada deployment production; mode ini mewajibkan
`OUTBOX_TARGET_URL` memakai HTTPS. HTTP tetap didukung untuk `local` dan `test`.

Setiap request memakai compact JSON deterministik (`sort_keys=True`) dan body bytes yang sama
untuk pengiriman serta HMAC-SHA256. Signature `v1=<hex>` pada
`X-Retriv-Signature` dihitung atas
`{unix_timestamp}.{event_id}.<body>`. Key dipilih melalui `X-Retriv-Key-Id`, sedangkan timestamp
dikirim melalui `X-Retriv-Timestamp`. Receiver harus memverifikasi signature dengan raw body,
menerapkan kebijakan freshness timestamp-nya sendiri, dan menangani `Idempotency-Key` secara
idempotent.

Dengan dummy receiver aktif, event yang diterima dapat dilihat melalui:

```bash
curl http://127.0.0.1:8081/events
```

`published` berarti receiver sudah mengembalikan response `2xx`; status pemrosesan RAG atau
agents harus dikelola oleh service masing-masing. Event published dibersihkan setelah retention
`OUTBOX_RETENTION_DAYS` hari.

## API Quickstart

Login memakai OAuth2 form encoding:

```bash
TOKEN=$(curl --silent --fail \
  -X POST http://127.0.0.1:8000/v1/auth/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'username=admin' \
  --data-urlencode 'password=YOUR_PASSWORD' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

List workspace:

```bash
curl --fail \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/workspaces
```

Upload document. Ganti `WORKSPACE_ID` dengan UUID dari response workspace:

```bash
curl --fail \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@./document.pdf;type=application/pdf' \
  http://127.0.0.1:8000/v1/workspaces/WORKSPACE_ID/documents
```

Endpoint yang tersedia:

```text
POST   /v1/auth/token
POST   /v1/admin/users
POST   /v1/admin/users/{user_id}/password
POST   /v1/admin/workspaces
GET    /v1/admin/workspaces/{workspace_id}/members
PUT    /v1/admin/workspaces/{workspace_id}/members/{user_id}
DELETE /v1/admin/workspaces/{workspace_id}/members/{user_id}
GET    /v1/workspaces
GET    /v1/workspaces/{workspace_id}/members
PUT    /v1/workspaces/{workspace_id}/members/{user_id}
DELETE /v1/workspaces/{workspace_id}/members/{user_id}
POST   /v1/workspaces/{workspace_id}/documents
GET    /v1/workspaces/{workspace_id}/documents
GET    /v1/workspaces/{workspace_id}/documents/{document_id}
GET    /v1/workspaces/{workspace_id}/documents/{document_id}/download
DELETE /v1/workspaces/{workspace_id}/documents/{document_id}
GET    /health/live
GET    /health/ready
```

Dokumentasi OpenAPI tersedia di `http://127.0.0.1:8000/docs`.

Admin user creation returns a generated permanent password once with `Cache-Control: no-store`.
Only the Argon2id hash is stored. If the response is lost, an administrator can generate a new
password. Email invitations and password reset delivery are planned for a later milestone.

## Development Commands

Install dependency dan jalankan semua test:

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run mypy app provisioning outbox alembic tests
```

Schema migration dijalankan dari host sebelum API dimulai. Untuk menjalankan pemeriksaan drift
terhadap PostgreSQL pada stack lokal:

```bash
uv run --env-file .env -- alembic check
```

## Storage Flow

```text
Client
  -> FastAPI JWT/authentication
  -> bounded temporary file pada runtime aplikasi
  -> ClamAV INSTREAM scan
  -> PDF/OOXML validation dan SHA-256
  -> private MinIO bucket
  -> PostgreSQL metadata + transactional outbox event
```

Object key tidak menggunakan nama file dari user:

```text
workspaces/{workspace_id}/objects/{sha256}/{document_id}
```

FastAPI melakukan streaming download dari MinIO. File asli tidak disimpan pada filesystem
permanen aplikasi; hanya temporary staging yang digunakan selama upload.

## Next Milestone

Milestone berikutnya dapat menambahkan processing job terpisah untuk ekstraksi teks,
OCR `ind+eng`, provenance per halaman/sheet, chunking, dan indexing. Raw object dan
workspace-scoped document contract pada MVP ini dipertahankan sebagai source of truth.
