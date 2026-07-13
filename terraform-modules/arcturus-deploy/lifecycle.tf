locals {
  up_script   = "${path.module}/scripts/up.sh"
  down_script = "${path.module}/scripts/down.sh"
}

resource "null_resource" "manage_stack" {
  count = var.skip_restart ? 0 : 1

  depends_on = [local_file.compose_yaml, null_resource.deploy_nginx_conf]

  triggers = {
    compose_hash   = md5(var.compose_content)
    nginx_hash     = md5(local.nginx_conf_content)
    deploy_trigger = var.deploy_trigger
  }

  provisioner "local-exec" {
    command = "bash ${local.up_script} ${var.app_name} ${var.stacks_base_dir}"
  }
}

resource "null_resource" "destroy_stack" {
  triggers = {
    app_name       = var.app_name
    stacks_base    = var.stacks_base_dir
    stack_dir      = local.stack_dir
    compose_exists = fileexists(local.compose_path)
    down_script    = local.down_script
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${self.triggers.down_script} ${self.triggers.app_name} ${self.triggers.stacks_base}"
  }
}
