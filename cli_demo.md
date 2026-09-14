# S3 CLI demo commands

Manual, copy-pasteable walkthrough of the same operations `run_test.py`
automates. Each step shows the command that performs an action, then a
command that shows its result.

Requires the `aws` CLI. Every command below is self-contained (no shell
variables) — it uses a fixed `my-bucket` bucket, a `seaweedfs` AWS CLI
profile, and the endpoint `http://localhost:8333` directly. Adjust those
literals if your setup differs.

## Setup

```bash
aws configure set aws_access_key_id YOUR_ACCESS_KEY --profile seaweedfs
aws configure set aws_secret_access_key YOUR_SECRET_KEY --profile seaweedfs
aws configure set region us-east-1 --profile seaweedfs

# SeaweedFS (and most self-hosted stores) expect path-style addressing.
aws configure set s3.addressing_style path --profile seaweedfs
```

## 1. create_bucket

```bash
aws s3api create-bucket --profile seaweedfs --bucket my-bucket --object-lock-enabled-for-bucket --endpoint-url http://localhost:8333

aws s3api put-bucket-versioning --profile seaweedfs --bucket my-bucket --versioning-configuration Status=Enabled --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api list-buckets --profile seaweedfs --endpoint-url http://localhost:8333 --query "Buckets[?Name=='my-bucket']"
```

## 2. put_get_object

```bash
echo -n "hello from s3-compat-test" > hello.txt
aws s3api put-object --profile seaweedfs --bucket my-bucket --key hello.txt --body hello.txt --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api get-object --profile seaweedfs --bucket my-bucket --key hello.txt --endpoint-url http://localhost:8333 hello-downloaded.txt
cat hello-downloaded.txt
```

## 3. list_objects

```bash
aws s3api list-objects-v2 --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333
```

## 4. multipart_upload

```bash
head -c 5242880 /dev/zero | tr '\0' 'A' > part1.bin
head -c 5242880 /dev/zero | tr '\0' 'B' > part2.bin

aws s3api create-multipart-upload --profile seaweedfs --bucket my-bucket --key big.bin --endpoint-url http://localhost:8333
```

This prints an `UploadId` — copy it into the commands below (replacing `PASTE_UPLOAD_ID`):

```bash
aws s3api upload-part --profile seaweedfs --bucket my-bucket --key big.bin --part-number 1 --upload-id PASTE_UPLOAD_ID --body part1.bin --endpoint-url http://localhost:8333

aws s3api upload-part --profile seaweedfs --bucket my-bucket --key big.bin --part-number 2 --upload-id PASTE_UPLOAD_ID --body part2.bin --endpoint-url http://localhost:8333
```

Each `upload-part` call prints an `ETag` — copy both into the command below (replacing `PASTE_ETAG1` / `PASTE_ETAG2`):

```bash
aws s3api complete-multipart-upload --profile seaweedfs --bucket my-bucket --key big.bin --upload-id PASTE_UPLOAD_ID --multipart-upload '{"Parts":[{"ETag":PASTE_ETAG1,"PartNumber":1},{"ETag":PASTE_ETAG2,"PartNumber":2}]}' --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api head-object --profile seaweedfs --bucket my-bucket --key big.bin --endpoint-url http://localhost:8333
```

## 5. presigned_url_get

```bash
aws s3 presign s3://my-bucket/hello.txt --profile seaweedfs --endpoint-url http://localhost:8333 --expires-in 300
```

The URL printed *is* the result — paste it into a browser or `curl` it to confirm it downloads the object.

## 6. presigned_url_put

The `aws` CLI can only presign GET requests. Use boto3 (the same client `run_test.py` uses) for a presigned PUT:

```bash
python3 - <<'PY'
import boto3
from botocore.client import Config

s3 = boto3.client(
    "s3", endpoint_url="http://localhost:8333", profile_name="seaweedfs",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": "my-bucket", "Key": "presigned-upload.txt"},
    ExpiresIn=300,
)
print(url)
PY
```

Show result: `curl -X PUT --data-binary @hello.txt "<url printed above>"`, then list the bucket to confirm the object landed.

## 7. bucket_versioning

```bash
aws s3api get-bucket-versioning --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333

echo -n "v1" > v1.txt
echo -n "v2" > v2.txt
aws s3api put-object --profile seaweedfs --bucket my-bucket --key file.txt --body v1.txt --endpoint-url http://localhost:8333
aws s3api put-object --profile seaweedfs --bucket my-bucket --key file.txt --body v2.txt --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api list-object-versions --profile seaweedfs --bucket my-bucket --prefix file.txt --endpoint-url http://localhost:8333
```

## 8. object_tagging

```bash
aws s3api put-object-tagging --profile seaweedfs --bucket my-bucket --key hello.txt --tagging '{"TagSet":[{"Key":"env","Value":"test"}]}' --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api get-object-tagging --profile seaweedfs --bucket my-bucket --key hello.txt --endpoint-url http://localhost:8333
```

## 9. bucket_lifecycle

```bash
aws s3api put-bucket-lifecycle-configuration --profile seaweedfs --bucket my-bucket --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "expire-old-objects",
        "Filter": {"Prefix": "basic/"},
        "Status": "Enabled",
        "Expiration": {"Days": 365}
      }
    ]
  }' --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api get-bucket-lifecycle-configuration --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333
```

## 10. object_lock

```bash
aws s3api put-object-lock-configuration --profile seaweedfs --bucket my-bucket --object-lock-configuration '{"ObjectLockEnabled":"Enabled"}' --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api get-object-lock-configuration --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333
```

## 11. bucket_policy

```bash
aws s3api put-bucket-policy --profile seaweedfs --bucket my-bucket --policy '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "AllowListBucket",
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:ListBucket",
        "Resource": "arn:aws:s3:::my-bucket"
      }
    ]
  }' --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api get-bucket-policy --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333
```

## 12. cleanup

```bash
# Delete object versions
aws s3api list-object-versions --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333 --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json > versions.json
aws s3api delete-objects --profile seaweedfs --bucket my-bucket --delete file://versions.json --endpoint-url http://localhost:8333

# Delete bucket
aws s3api delete-bucket --profile seaweedfs --bucket my-bucket --endpoint-url http://localhost:8333
```

Show result:

```bash
aws s3api list-buckets --profile seaweedfs --endpoint-url http://localhost:8333 --query "Buckets[?Name=='my-bucket']"
# empty result means the bucket is gone
```
