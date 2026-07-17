# GitHub Actions runners

Arcturus does not manage GitHub runner registration as part of the public control plane. Prefer GitHub-hosted runners. When a self-hosted runner is necessary, use a dedicated account and isolate Buildah/container storage per job.

The service blueprint uses rootless Buildah and must not require a privileged runner or access to the production host Podman socket. Registration tokens, runner credentials, generated configuration, and work directories are host secrets and must never be committed.
