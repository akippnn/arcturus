terraform {
  required_version = ">= 1.5.0"
}

resource "terraform_data" "install_deployer" {
  triggers_replace = {
    requirements_sha256 = filesha256("${var.arcturus_source_dir}/deploy/requirements.txt")
    installer_sha256    = filesha256("${var.arcturus_source_dir}/deploy/install-host.sh")
    unit_sha256         = filesha256("${var.arcturus_source_dir}/deploy/arcturus-deployer@.service")
    podman_api_sha256   = filesha256("${var.arcturus_source_dir}/deploy/arcturus-podman-api.service")
    configuration = sha256(jsonencode({
      host_user         = var.host_user
      runner_address    = var.runner_bind_address
      runner_cidr       = var.runner_cidr
      allowed_bind_root = var.allowed_bind_roots
    }))
  }

  provisioner "local-exec" {
    command = join(" ", compact(concat([
      "bash",
      jsonencode("${var.arcturus_source_dir}/deploy/install-host.sh"),
      "--source-dir",
      jsonencode("${var.arcturus_source_dir}/deploy"),
      var.host_user == null ? "" : "--host-user ${jsonencode(var.host_user)}",
      var.runner_bind_address == "" ? "" : "--listen-address ${jsonencode(var.runner_bind_address)}",
      var.runner_cidr == "" ? "" : "--runner-cidr ${jsonencode(var.runner_cidr)}",
    ], [for root in var.allowed_bind_roots : "--allowed-bind-root ${jsonencode(root)}"])))
  }
}

output "deployer_endpoints" {
  value = compact([
    "http://127.0.0.1:9090",
    var.runner_bind_address == "" ? "" : "http://${var.runner_bind_address}:9090",
  ])
}
