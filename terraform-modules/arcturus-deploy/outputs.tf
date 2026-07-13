output "stack_name" {
  value       = var.app_name
  description = "The stack name used in Dockge."
}

output "ingress_domain" {
  value       = var.domain
  description = "The public domain configured for this stack."
}

output "compose_path" {
  value       = local.compose_path
  description = "Path to the deployed compose.yaml on the host."
}

output "protected" {
  value       = var.protect_compose
  description = "Whether the compose file is protected (read-only + .dockge-protect marker)."
}
