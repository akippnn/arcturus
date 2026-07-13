locals {
  nginx_conf_content = <<-EOF
server {
    listen 443 ssl;
    server_name ${var.domain};
    ssl_certificate /etc/letsencrypt/live/${var.cert_domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${var.cert_domain}/privkey.pem;

    client_max_body_size 10G;
    proxy_intercept_errors on;
    error_page 502 503 504 =503 /@offline;

    location @offline {
        add_header X-Arcturus-Status "offline" always;
        add_header 'Access-Control-Allow-Origin' '*' always;
        rewrite ^ /index.html break;
    }

    location / {
        set $upstream_target "${var.target_url}";
        proxy_pass $upstream_target;

        proxy_set_header Host ${var.tier == "simple" ? local.target_host : "$host"};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port $server_port;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        ${var.tier == "simple" ? "proxy_buffering on;" : "proxy_buffering off; proxy_request_buffering off;"}

        # Custom Location Config Injection
        ${var.custom_nginx_location_config}
    }

    # Custom Server Config Injection
    ${var.custom_nginx_server_config}
}
EOF
}

resource "local_file" "nginx_conf" {
  content  = local.nginx_conf_content
  filename = "${path.module}/.terraform_generated/${var.app_name}.conf"

  lifecycle {
    precondition {
      condition = (
        var.domain == var.cert_domain
        ? var.app_name == "example-portal"
        : can(regex("[.]${replace(var.cert_domain, ".", "[.]")}$", var.domain))
      )
      error_message = "domain must be within cert_domain, and only example-portal may own the apex."
    }
  }
}

resource "null_resource" "deploy_nginx_conf" {
  triggers = {
    conf_hash         = md5(local.nginx_conf_content)
    portal_vhosts_dir = var.portal_vhosts_dir
    app_name          = var.app_name
    nginx_container   = var.nginx_container
  }

  provisioner "local-exec" {
    command = <<-EOF
      CONTAINER_ID=$(docker create -v ${self.triggers.portal_vhosts_dir}:/vhosts alpine)
      docker cp ${local_file.nginx_conf.filename} $CONTAINER_ID:/vhosts/${self.triggers.app_name}.conf
      docker rm $CONTAINER_ID
      docker exec ${lookup(self.triggers, "nginx_container", "portal-nginx")} nginx -s reload 2>&1 || echo "nginx reload deferred (portal may be starting)"
    EOF
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOF
      CONTAINER_ID=$(docker create -v ${self.triggers.portal_vhosts_dir}:/vhosts alpine)
      docker exec $CONTAINER_ID rm -f /vhosts/${self.triggers.app_name}.conf 2>/dev/null || true
      docker rm $CONTAINER_ID 2>/dev/null || true
      docker exec ${lookup(self.triggers, "nginx_container", "portal-nginx")} nginx -s reload 2>&1 || true
    EOF
  }
}
