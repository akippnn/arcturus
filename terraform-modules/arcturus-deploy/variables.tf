terraform {
  required_version = ">= 1.2.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4.0"
    }
  }
}

variable "app_name" {
  type        = string
  description = "Application slug (lowercase, no spaces). Used as the stack name in Dockge and directory name."
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_-]{0,62}$", var.app_name))
    error_message = "app_name must be a lowercase slug using letters, numbers, underscores, and hyphens."
  }
}

variable "domain" {
  type        = string
  description = "The target public domain (e.g., app.example.org)."
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", var.domain))
    error_message = "domain must be a valid lowercase DNS name."
  }
}

variable "tier" {
  type        = string
  default     = "complex"
  description = "Tier: 'simple' (R2 bucket proxy) or 'complex' (internal proxy)."
  validation {
    condition     = contains(["simple", "complex"], var.tier)
    error_message = "Valid values for tier are 'simple' or 'complex'."
  }
}

variable "target_url" {
  type        = string
  description = "Target URL. For 'simple', this is the R2 bucket public URL. For 'complex', this is the internal Docker service URL (e.g., http://my-service:80)."
  validation {
    condition     = startswith(var.target_url, "http://") || startswith(var.target_url, "https://")
    error_message = "target_url must start with http:// or https://."
  }
}

variable "portal_vhosts_dir" {
  type        = string
  default     = "/srv/arcturus/portal/vhosts.d"
  description = "Path to the portal's nginx vhost config directory."
  validation {
    condition     = can(regex("^/[A-Za-z0-9_./-]+$", var.portal_vhosts_dir))
    error_message = "portal_vhosts_dir must be an absolute path without shell metacharacters."
  }
}

variable "stacks_base_dir" {
  type        = string
  default     = "/srv/arcturus/stacks"
  description = "Base directory where all application stacks live."
  validation {
    condition     = can(regex("^/[A-Za-z0-9_./-]+$", var.stacks_base_dir))
    error_message = "stacks_base_dir must be an absolute path without shell metacharacters."
  }
}

variable "protect_compose" {
  type        = bool
  default     = true
  description = "Mark the deployed compose.yaml as read-only (chmod 444) and place a .dockge-protect marker to prevent editing/deletion from Dockge."
}

variable "cert_domain" {
  type        = string
  default     = "example.org"
  description = "Domain whose Let's Encrypt cert to use (typically the wildcard cert domain)."
}

variable "compose_content" {
  type        = string
  description = "Content of the compose.yaml file to deploy. Passed from the calling project's compose.yaml via file()."
}

variable "skip_restart" {
  type        = bool
  default     = false
  description = "Skip the podman-compose restart step. Useful for initial deployments where you want to manually review first."
}

variable "nginx_container" {
  type        = string
  default     = "portal-nginx"
  description = "Name of the nginx container in the portal for config reload."
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.nginx_container))
    error_message = "nginx_container must be a valid container name."
  }
}

variable "deploy_trigger" {
  type        = string
  default     = ""
  description = "A unique trigger (e.g. Git commit SHA) to force container recreation on new builds."
  validation {
    condition     = var.deploy_trigger == "" || can(regex("^[0-9a-fA-F]{40}$", var.deploy_trigger))
    error_message = "deploy_trigger must be empty or a 40-character git SHA."
  }
}

variable "custom_nginx_server_config" {
  type        = string
  default     = ""
  description = "Custom Nginx configuration lines to inject inside the server block."
}

variable "custom_nginx_location_config" {
  type        = string
  default     = ""
  description = "Custom Nginx configuration lines to inject inside the main location / block."
}
