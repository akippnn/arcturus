---
title: Certbot runtime directory
kind: guide
lifecycle: operational
authority: Portal certificate runtime-file safety
summary: Keep Certbot credentials and generated state outside Git.
maintenance:
  - The portal certificate runtime layout or secret-handling procedure changes.
---

# Certbot Runtime Directory

This directory is a placeholder for certbot runtime files on the host.

Create these files/directories on the deployment host, not in Git:

- `cloudflare.ini`
- `conf/`

Keep real Cloudflare tokens, certificates, renewal state, and private keys out of
the repository.
