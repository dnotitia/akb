# Workload identity for Gateway and object storage

AKB supports two deployment profiles. Standalone `external_metering` keeps
direct model-provider credentials and either static S3 credentials or the
native AWS credential chain. `platform_hard` requires projected workload
tokens for both the model Gateway and S3 STS; it rejects static model, S3,
and audit-storage credentials at startup.

## Managed configuration

The control plane provisions the bucket and role before starting AKB. It
mounts two projected ServiceAccount tokens with distinct audiences and writes
the following non-secret settings to `app.yaml`:

```yaml
model_api_governance_mode: platform_hard
platform_gateway_base_url: https://gateway.example/v1
platform_gateway_token_file: /var/run/akb-identity/gateway/token
embed_base_url: https://gateway.example/v1
embed_model: text-embedding-3-small
embed_dimensions: 1536

s3_auth_mode: default_chain
s3_endpoint_url: https://storage.example
s3_public_url: https://files.example
s3_bucket: tenant-files
s3_region: us-east-1
s3_sts_endpoint_url: https://storage.example
s3_role_arn: arn:aws:iam:::role/tenant-files
s3_web_identity_token_file: /var/run/akb-identity/rgw/token
```

Every enabled embedding, chat, and rerank route must use the declared Gateway
base URL. A blank embedding or chat URL disables that route. Each logical
request reopens the Gateway token path and sends its current contents as the
Bearer token. Transport retries retain that request's idempotency key. Missing,
empty, or invalid token files prevent the outbound request.

Despite the `default_chain` selection, the managed profile installs only the
configured WebIdentity provider. It never selects AWS environment credentials,
shared profiles, instance metadata, or a different STS endpoint. AKB exchanges
the RGW token using unsigned `AssumeRoleWithWebIdentity`, caches the resulting
temporary session in memory, and reopens the token file when refreshing it.
Both S3 endpoint clients share this session. The token path may use Kubernetes
projection symlinks; no file handle is retained between reads.

S3 clients use SigV4 and custom endpoints use path-style addressing. Browser
upload/download URLs contain the temporary session token. Each URL is signed
with a fixed credential snapshot and its lifetime is capped at that snapshot's
remaining lifetime minus 60 seconds. API `expires_in` reports the actual cap.
Expired sessions and sessions with less than that safety margin cannot produce
a URL. See the [S3 presigned URL lifetime contract](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).

Managed startup performs `HeadBucket` and fails if identity exchange or bucket
access fails. It never creates a bucket. The role must authorize the required
object operations and `HeadBucket` on the provisioned bucket. RGW OIDC trust,
audience, role policy, and STS support are control-plane responsibilities; see
the [Ceph STS documentation](https://docs.ceph.com/en/latest/radosgw/STS/).

Remove `embed_api_key`, `llm_api_key`, `rerank_api_key`, `s3_access_key`, and
`s3_secret_key` from managed configuration, including `secret.yaml`. Dedicated
`audit.access_key` and `audit.secret_key` are also forbidden. An enabled audit
uploader uses the workload session, so the role must separately grant
`PutObject` on its pre-provisioned audit bucket. Leave `audit.bucket` blank for
file-only audit collection.

The process needs no Kubernetes API permissions. Token audiences, ServiceAccount
binding, projections, role/bucket policy, and the public endpoint's support for
temporary-session presigned GET/PUT must be configured by the deployment owner.
Ordinary AKB database and application secrets remain separate from these
upstream identity settings.

## Standalone compatibility

Existing MinIO/static configurations keep `s3_auth_mode: static` (the default)
with `s3_access_key` and `s3_secret_key` in `secret.yaml`. This profile retains
bucket creation on a missing bucket. Static mode never substitutes ambient AWS
credentials for empty configured keys.

For native cloud credentials, set `s3_auth_mode: default_chain` and remove both
static key fields. Leave all three explicit WebIdentity fields blank to use
the [native Boto3 credential providers](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html),
including cloud roles and AWS WebIdentity configuration. `s3_endpoint_url` and
`s3_public_url` may both be blank for AWS S3; set `s3_region` to the bucket's
region. This explicitly enables storage and cleanup workers without requiring
a custom endpoint. Buckets must already exist. Temporary credentials with a
known expiration receive the same presign cap; externally supplied session
tokens whose provider does not expose an expiration remain subject to the
provider's actual expiry.

A standalone RGW installation can also select `default_chain` and supply the
complete `s3_web_identity_token_file`, `s3_role_arn`, `s3_sts_endpoint_url` tuple.
That selects the explicit RGW provider without requiring a model Gateway.

The standalone Helm chart exposes `app.objectStorage.authMode`, `endpointUrl`,
`publicUrl`, `bucket`, and `region`. Native AWS role projection follows the
cluster's normal ServiceAccount configuration. Managed control planes provide
the complete `app.yaml` and the two audience-specific token projections.
