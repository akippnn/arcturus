locals {
  target_host  = replace(replace(var.target_url, "https://", ""), "http://", "")
  compose_path = "${var.stacks_base_dir}/${var.app_name}/compose.yaml"
  stack_dir    = "${var.stacks_base_dir}/${var.app_name}"
}
