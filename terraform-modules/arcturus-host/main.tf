terraform {
  required_version = ">= 1.5.0"
}

resource "terraform_data" "install_deployer" {
  triggers_replace = {
    requirements_sha256 = filesha256("${var.arcturus_source_dir}/deploy/requirements.txt")
    installer_sha256    = filesha256("${var.arcturus_source_dir}/deploy/install-host.sh")
    paths_sha256        = filesha256("${var.arcturus_source_dir}/deploy/arcturus_paths.py")
    unit_sha256         = filesha256("${var.arcturus_source_dir}/deploy/arcturus-deployer@.service")
    podman_api_sha256   = filesha256("${var.arcturus_source_dir}/deploy/arcturus-podman-api.service")
    configuration = sha256(jsonencode({
      host_user         = var.host_user
      runner_address    = var.runner_bind_address
      runner_cidr       = var.runner_cidr
      allowed_bind_root = var.allowed_bind_roots
      config_root       = var.config_root
      data_root         = var.data_root
      cache_root        = var.cache_root
      runtime_root      = var.runtime_root
      bin_dir           = var.bin_dir
      workload_root     = var.workload_root
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
      var.config_root == "" ? "" : "--config-root ${jsonencode(var.config_root)}",
      var.data_root == "" ? "" : "--data-root ${jsonencode(var.data_root)}",
      var.cache_root == "" ? "" : "--cache-root ${jsonencode(var.cache_root)}",
      var.runtime_root == "" ? "" : "--runtime-root ${jsonencode(var.runtime_root)}",
      var.bin_dir == "" ? "" : "--bin-dir ${jsonencode(var.bin_dir)}",
      var.workload_root == "" ? "" : "--workload-root ${jsonencode(var.workload_root)}",
    ], [for root in var.allowed_bind_roots : "--allowed-bind-root ${jsonencode(root)}"])))
  }
}

output "deployer_endpoints" {
  value = compact([
    "http://127.0.0.1:9090",
    var.runner_bind_address == "" ? "" : "http://${var.runner_bind_address}:9090",
  ])
}
