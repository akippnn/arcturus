# CI runners

Arcturus does not manage CI runner registration as part of the public control plane. Use a dedicated runner account and isolate build storage per job. The service blueprint uses rootless Buildah and does not require a privileged runner or access to the host Podman socket.

`config.example.yaml` is intentionally conservative. Generate a current configuration with your runner version and add capabilities only when a reviewed workflow requires them. Runner registration tokens and generated `.runner` files are host secrets and must never be committed.
