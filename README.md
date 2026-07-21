# seaweedfs-poc

A proof-of-concept SeaweedFS deployment (via Docker Compose) plus a script
that validates its S3-compatible API surface.

## Running SeaweedFS

```
docker compose -f seaweedfs-compose.yml up -d
```

This starts the master, volume, filer, S3, and admin webui services.
Volume data is persisted under `./seaweedfs/` (gitignored).

## Testing the S3 API

`run_test.py` runs a common set of S3 operations against a single
S3-compatible endpoint (MinIO, SeaweedFS, RustFS, Ceph RGW, Garage,
AIStor, ...) and reports which operations succeeded, failed, or errored.

This does **not** replace testing against your real internal workflows —
it validates the underlying S3 API surface those workflows likely depend
on (uploads, multipart, presigned URLs, versioning, tagging, lifecycle
rules, object lock). Use the results as a first-pass signal, not a final
acceptance test.

### Usage

1. Copy `.env.example` to `.env` and fill in the connection details for
   the endpoint you want to test.

2. Run:
   ```
   pip install boto3 python-dotenv --break-system-packages
   python3 run_test.py
   ```

3. Read the summary table printed at the end. Each row is an operation,
   with `PASS` / `FAIL` / `ERROR` / `SKIP`. Full details (exceptions,
   response snippets) are printed above the table and also written to
   `s3_compat_results.json`.

### Notes

- The script creates and deletes its own throwaway bucket (default:
  `s3-compat-test-<run-id>`), so it should be safe to run against a live
  instance, but don't point it at production.
- Some operations (object lock, bucket replication, notifications) need
  bucket-level configuration at creation time or extra setup; where an
  operation legitimately can't be tested generically, it's marked `SKIP`
  rather than `FAIL`.
- boto3 talks the standard AWS S3 API. Path-style addressing is forced
  on since most self-hosted S3-compatible stores expect it
  (virtual-hosted style requires DNS wildcard setup that most
  local/self-hosted setups don't have).
