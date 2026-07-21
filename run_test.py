#!/usr/bin/env python3
"""
run_test.py

Runs a common set of S3 operations against a single S3-compatible endpoint
(MinIO, SeaweedFS, RustFS, Ceph RGW, Garage, AIStor, ...) and reports which
operations succeeded, failed, or errored.

This does NOT replace testing against your real internal workflows --
it validates the underlying S3 API surface that those workflows likely
depend on (uploads, multipart, presigned URLs, versioning, tagging,
lifecycle rules, object lock). Use the results as a first-pass signal,
not a final acceptance test.

USAGE
-----
1. Copy .env.example to .env and fill in the connection details for the
   endpoint you want to test.

2. Run:
       pip install boto3 python-dotenv --break-system-packages
       python3 run_test.py

3. Read the summary table printed at the end. Each row is an operation,
   with PASS / FAIL / ERROR / SKIP. Full details (exceptions, response
   snippets) are printed above the table and also written to
   s3_compat_results.json.

NOTES
-----
- The script creates and deletes its own throwaway bucket (default:
  "s3-compat-test-<timestamp>"), so it should be safe to run against a
  live instance, but don't point it at production.
- Some operations (object lock, bucket replication, notifications) need
  bucket-level configuration at creation time or extra setup; where an
  operation legitimately can't be tested generically, it's marked SKIP
  rather than FAIL.
- boto3 talks standard AWS S3 API. Path-style addressing is forced on
  since most self-hosted S3-compatible stores expect it (virtual-hosted
  style requires DNS wildcard setup that most local/self-hosted setups
  don't have).
"""

import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 1. ENDPOINT CONFIG -- read from environment / .env
# ---------------------------------------------------------------------------
REQUIRED_VARS = ["S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY"]


def load_config():
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}")
        print("Set them in .env (see .env.example) or export them before running.")
        sys.exit(1)
    return {
        "name": os.environ.get("S3_NAME", "endpoint"),
        "endpoint_url": os.environ["S3_ENDPOINT"],
        "access_key": os.environ["S3_ACCESS_KEY"],
        "secret_key": os.environ["S3_SECRET_KEY"],
        "region": os.environ.get("S3_REGION", "us-east-1"),
    }


RUN_ID = uuid.uuid4().hex[:8]
BUCKET_NAME = f"s3-compat-test-{RUN_ID}"
RESULTS_FILE = "s3_compat_results.json"

# ---------------------------------------------------------------------------
# Test registry -- add/remove operations here as needed
# ---------------------------------------------------------------------------
# Each test function receives (s3_client, bucket_name) and either returns
# a short success detail string, raises an exception (-> FAIL), or returns
# the sentinel "SKIP:<reason>" if the test genuinely cannot run generically.

def test_create_bucket(s3, bucket):
    s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    return "bucket created"


def test_put_get_object(s3, bucket):
    key = "basic/hello.txt"
    body = b"hello from s3-compat-test"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    resp = s3.get_object(Bucket=bucket, Key=key)
    fetched = resp["Body"].read()
    assert fetched == body, "downloaded content did not match uploaded content"
    return f"put/get roundtrip ok ({len(body)} bytes)"


def test_list_objects(s3, bucket):
    resp = s3.list_objects_v2(Bucket=bucket)
    keys = [o["Key"] for o in resp.get("Contents", [])]
    assert "basic/hello.txt" in keys, "expected object not found in listing"
    return f"listed {len(keys)} object(s)"


def test_multipart_upload(s3, bucket):
    key = "multipart/big.bin"
    part_size = 5 * 1024 * 1024  # 5 MiB minimum part size for most S3 impls
    num_parts = 2
    mpu = s3.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = mpu["UploadId"]
    parts = []
    try:
        for i in range(1, num_parts + 1):
            data = (chr(64 + i).encode() * part_size)
            part = s3.upload_part(
                Bucket=bucket, Key=key, PartNumber=i,
                UploadId=upload_id, Body=data,
            )
            parts.append({"ETag": part["ETag"], "PartNumber": i})
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    head = s3.head_object(Bucket=bucket, Key=key)
    expected_size = part_size * num_parts
    assert head["ContentLength"] == expected_size, (
        f"expected {expected_size} bytes, got {head['ContentLength']}"
    )
    return f"multipart upload ok ({num_parts} parts, {expected_size} bytes)"


def test_presigned_url_get(s3, bucket):
    key = "basic/hello.txt"
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
    )
    assert url.startswith("http"), "presigned URL generation returned unexpected value"
    return "presigned GET URL generated (not fetched over HTTP -- generation only)"


def test_presigned_url_put(s3, bucket):
    key = "basic/presigned-upload.txt"
    url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
    )
    assert url.startswith("http"), "presigned PUT URL generation returned unexpected value"
    return "presigned PUT URL generated (not fetched over HTTP -- generation only)"


def test_bucket_versioning(s3, bucket):
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    resp = s3.get_bucket_versioning(Bucket=bucket)
    assert resp.get("Status") == "Enabled", "versioning did not report Enabled after being set"
    # write two versions of the same key and confirm both are listed
    key = "versioned/file.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=b"v1")
    s3.put_object(Bucket=bucket, Key=key, Body=b"v2")
    versions = s3.list_object_versions(Bucket=bucket, Prefix=key)
    count = len(versions.get("Versions", []))
    assert count >= 2, f"expected >=2 versions, found {count}"
    return f"versioning enabled and confirmed ({count} versions of test key)"


def test_object_tagging(s3, bucket):
    key = "basic/hello.txt"
    s3.put_object_tagging(
        Bucket=bucket, Key=key,
        Tagging={"TagSet": [{"Key": "env", "Value": "test"}]},
    )
    resp = s3.get_object_tagging(Bucket=bucket, Key=key)
    tags = {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}
    assert tags.get("env") == "test", "tag not found after being set"
    return "object tagging set and verified"


def test_bucket_lifecycle(s3, bucket):
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-old-objects",
                    "Filter": {"Prefix": "basic/"},
                    "Status": "Enabled",
                    "Expiration": {"Days": 365},
                }
            ]
        },
    )
    resp = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    rules = resp.get("Rules", [])
    assert any(r.get("ID") == "expire-old-objects" for r in rules), (
        "lifecycle rule not found after being set"
    )
    return "lifecycle rule (expiration) set and verified"


def test_object_lock(s3, bucket):
    # Object lock generally must be enabled AT bucket creation time, which
    # this generic script does not do (bucket is created without it above).
    # We attempt to read lock config; if unsupported or not enabled, mark SKIP
    # rather than FAIL, since this needs a dedicated bucket to test properly.
    try:
        s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration={
            'ObjectLockEnabled': 'Enabled'
        })

        return "object lock configuration set"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"):
            return "SKIP: object lock not enabled on this bucket (must be set at bucket creation -- create a dedicated locked bucket to test fully)"
        raise


def test_bucket_policy(s3, bucket):
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowListBucket",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    resp = s3.get_bucket_policy(Bucket=bucket)
    assert "AllowListBucket" in resp["Policy"], "policy content mismatch after being set"
    return "bucket policy set and verified"



def test_delete_objects_and_bucket(s3, bucket):
    # cleanup -- also doubles as a test of batch delete
    to_delete = []
    used_version_api = False
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            for v in page.get("Versions", []):
                to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for m in page.get("DeleteMarkers", []):
                to_delete.append({"Key": m["Key"], "VersionId": m["VersionId"]})
        used_version_api = True
    except ClientError:
        # Some S3-compatible stores (e.g. Garage) may not fully support
        # list_object_versions -- fall back to a plain listing.
        pass

    if not used_version_api:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                to_delete.append({"Key": obj["Key"]})

    errors = []
    if to_delete:
        # batch delete in chunks of 1000 (S3 API limit)
        for i in range(0, len(to_delete), 1000):
            chunk = to_delete[i : i + 1000]
            resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk})
            errors.extend(resp.get("Errors", []))

    if errors:
        detail = "; ".join(f"{e['Key']}: {e.get('Code')}" for e in errors[:5])
        raise RuntimeError(f"delete_objects reported {len(errors)} error(s): {detail}")

    s3.delete_bucket(Bucket=bucket)
    return f"cleaned up {len(to_delete)} object-version(s) and deleted bucket"



# Order matters: bucket must exist before objects can be created, and
# cleanup must run last.
TEST_SEQUENCE = [
    ("create_bucket", test_create_bucket),
    ("put_get_object", test_put_get_object),
    ("list_objects", test_list_objects),
    ("multipart_upload", test_multipart_upload),
    ("presigned_url_get", test_presigned_url_get),
    ("presigned_url_put", test_presigned_url_put),
    ("bucket_versioning", test_bucket_versioning),
    ("object_tagging", test_object_tagging),
    ("bucket_lifecycle", test_bucket_lifecycle),
    ("object_lock", test_object_lock),
    ("bucket_policy", test_bucket_policy),
    ("cleanup", test_delete_objects_and_bucket),
]


def make_client(cfg):
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint_url"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        region_name=cfg.get("region", "us-east-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def run_for_endpoint(cfg):
    name = cfg["name"]
    print(f"\n{'=' * 70}\nTesting: {name}  ({cfg['endpoint_url']})\n{'=' * 70}")
    results = {}
    try:
        s3 = make_client(cfg)
    except Exception as e:
        print(f"  [FATAL] could not create client: {e}")
        for test_name, _ in TEST_SEQUENCE:
            results[test_name] = {"status": "ERROR", "detail": f"client init failed: {e}"}
        return results

    for test_name, fn in TEST_SEQUENCE:
        try:
            detail = fn(s3, BUCKET_NAME)
            if isinstance(detail, str) and detail.startswith("SKIP:"):
                status = "SKIP"
                detail = detail[len("SKIP:"):].strip()
            else:
                status = "PASS"
            print(f"  [{status}] {test_name}: {detail}")
            results[test_name] = {"status": status, "detail": detail}
        except EndpointConnectionError as e:
            print(f"  [ERROR] {test_name}: could not connect -- {e}")
            results[test_name] = {"status": "ERROR", "detail": f"connection error: {e}"}
            # if we can't connect at all, no point trying further tests
            remaining = [t for t, _ in TEST_SEQUENCE if t not in results]
            for r in remaining:
                results[r] = {"status": "ERROR", "detail": "skipped: prior connection error"}
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "Unknown")
            msg = e.response.get("Error", {}).get("Message", str(e))
            print(f"  [FAIL] {test_name}: {code} -- {msg}")
            results[test_name] = {"status": "FAIL", "detail": f"{code}: {msg}"}
        except AssertionError as e:
            print(f"  [FAIL] {test_name}: {e}")
            results[test_name] = {"status": "FAIL", "detail": str(e)}
        except Exception as e:
            print(f"  [ERROR] {test_name}: {e}")
            results[test_name] = {"status": "ERROR", "detail": f"{type(e).__name__}: {e}"}
            if test_name != "cleanup":
                print(f"           (full trace below)")
                traceback.print_exc()

    return results


def print_summary_table(name, results):
    test_names = [t for t, _ in TEST_SEQUENCE]
    name_width = max(len(t) for t in test_names) + 2
    col_width = max(12, len(name) + 2)

    header = "Operation".ljust(name_width) + name.ljust(col_width)
    print("\n" + "=" * len(header))
    print("SUMMARY")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for t in test_names:
        status = results.get(t, {}).get("status", "N/A")
        print(t.ljust(name_width) + status.ljust(col_width))
    print("=" * len(header))
    print("PASS = worked as expected | FAIL = ran but behaved incorrectly / rejected")
    print("ERROR = connection or unexpected exception | SKIP = not generically testable")


def main():
    cfg = load_config()

    print(f"Run ID: {RUN_ID}")
    print(f"Bucket name: {BUCKET_NAME}")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")

    results = run_for_endpoint(cfg)
    print_summary_table(cfg["name"], results)

    with open(RESULTS_FILE, "w") as f:
        json.dump(
            {
                "run_id": RUN_ID,
                "bucket_name": BUCKET_NAME,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "endpoint": cfg["name"],
                "results": results,
            },
            f,
            indent=2,
        )
    print(f"\nFull results written to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
