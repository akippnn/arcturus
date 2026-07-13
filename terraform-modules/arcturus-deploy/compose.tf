resource "local_file" "compose_yaml" {
  content  = var.compose_content
  filename = local.compose_path
}

resource "local_file" "dockge_protect" {
  count = var.protect_compose ? 1 : 0

  content = jsonencode({
    protected        = true
    managed_by       = "terraform"
    compose_readonly = true
    delete_protected = true
    last_deployed    = var.deploy_trigger != "" ? var.deploy_trigger : timestamp()
  })
  filename = "${local.stack_dir}/.dockge-protect"
}

resource "null_resource" "seal_compose" {
  count = var.protect_compose ? 1 : 0

  depends_on = [local_file.compose_yaml, local_file.dockge_protect]

  triggers = {
    compose_path = local.compose_path
    stack_dir    = local.stack_dir
  }

  provisioner "local-exec" {
    command = "chmod 444 ${self.triggers.compose_path}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "rm -f ${self.triggers.compose_path} ${self.triggers.stack_dir}/.dockge-protect"
  }
}
