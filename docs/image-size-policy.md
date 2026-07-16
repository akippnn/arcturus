# Release image-size policy

Arcturus exposes an authenticated pre-upload policy endpoint so CI can reject oversized release images before sending layers to a container registry.

## Default policy

The default maximum is **805,306,368 bytes (768 MiB) per uncompressed local image**. Configure a different positive integer in the deployer environment only after reviewing the registry and host capacity:

```ini
ARCTURUS_MAX_IMAGE_SIZE_BYTES=805306368
```

Restart the Arcturus deployment API after changing the value.

## Endpoint

```http
POST /v1/image-policy
Authorization: Bearer <service-scoped deployment token>
Content-Type: application/json
```

```json
{
  "service": "example-portal",
  "image": "registry.example.org/example/portal:<revision>",
  "size_bytes": 314572800
}
```

An accepted image receives HTTP `200`. An image over the limit receives HTTP `413` with code `image_too_large` and the measured and allowed byte counts.

This endpoint does not receive or proxy image data. CI measures the completed local image, checks the host policy, and only then begins `buildah push`. Digest-pinned deployment remains unchanged.

Projects should also enforce a local fail-safe limit so an older or temporarily unavailable Arcturus host cannot expose the registry to an accidental oversized upload.
