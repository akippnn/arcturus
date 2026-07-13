variable "arcturus_source_dir" {
  description = "Absolute host path to the Arcturus repository."
  type        = string
}

variable "runner_bind_address" {
  description = "Private host address reachable from CI runners (normally Tailscale)."
  type        = string
  default     = ""
}

variable "runner_cidr" {
  description = "Trusted source CIDR when a private runner listener is enabled."
  type        = string
  default     = ""
}

variable "host_user" {
  description = "Rootless account that owns the Arcturus user services."
  type        = string
  default     = null
  nullable    = true
}

variable "allowed_bind_roots" {
  description = "Host roots that release manifests may bind mount."
  type        = list(string)
  default     = []
}
