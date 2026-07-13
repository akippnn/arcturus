# Optional ingress compatibility example

This directory is an operator-owned nginx/certbot/CrowdSec example. It is not required by the Arcturus release engine and is not an application lifecycle owner.

Before use:

1. copy `.example.env` to an untracked `.env`
2. replace all image placeholders with reviewed `image@sha256:digest` references
3. set `CONTAINER_DNS_RESOLVER` to the DNS/gateway address of the external Podman routing network
4. create `internal_routing`
5. provision the Cloudflare DNS credential file with mode `0600`
6. initialize CrowdSec configuration/state at runtime rather than committing generated files
7. ensure generated vhosts and logs remain untracked

The router writes vhosts into `config/nginx/vhosts.d`. TLS material and Cloudflare credentials remain operator secrets.
