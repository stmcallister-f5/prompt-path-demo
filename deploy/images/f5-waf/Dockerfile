# NGINX Plus + F5 WAF for NGINX v5 module (amd64 only). F5 doesn't publish this
# image; build it with your NGINX One cert and key (passed as build secrets, so
# they are not stored in the image):
#
#   make waf-image    (compiles booth-policy.json first, then builds this)
FROM debian:bookworm

# Pin a matching set. The repo can carry a newer app-protect-plugin than the
# latest module accepts, which makes an unpinned install fail.
ARG WAF_MODULE_VERSION=37.2+5.777.1-1~bookworm
ARG WAF_PLUGIN_VERSION=6.32.0-1~bookworm

RUN --mount=type=secret,id=nginx-crt,dst=/etc/ssl/nginx/nginx-repo.crt,mode=0644 \
    --mount=type=secret,id=nginx-key,dst=/etc/ssl/nginx/nginx-repo.key,mode=0644 \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends apt-transport-https lsb-release ca-certificates wget gnupg2 debian-archive-keyring; \
    wget -qO - https://cs.nginx.com/static/keys/nginx_signing.key | gpg --dearmor > /usr/share/keyrings/nginx-archive-keyring.gpg; \
    codename="$(lsb_release -cs)"; \
    echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] https://pkgs.nginx.com/plus/debian ${codename} nginx-plus" > /etc/apt/sources.list.d/nginx-plus.list; \
    echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] https://pkgs.nginx.com/app-protect-x-plus/debian ${codename} nginx-plus" > /etc/apt/sources.list.d/nginx-app-protect.list; \
    wget -qP /etc/apt/apt.conf.d https://cs.nginx.com/static/files/90pkgs-nginx; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      app-protect-module-plus=${WAF_MODULE_VERSION} \
      nginx-plus-module-appprotect=${WAF_MODULE_VERSION} \
      app-protect-plugin=${WAF_PLUGIN_VERSION}; \
    rm -f /etc/apt/apt.conf.d/90pkgs-nginx /etc/apt/sources.list.d/nginx-*.list; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf /dev/stdout /var/log/nginx/access.log; \
    ln -sf /dev/stderr /var/log/nginx/error.log

# Booth policy: the default policy plus 429 as an allowed response code.
# Compiled by `make waf-image` (waf-compiler image) before this build runs.
COPY booth-policy.tgz /opt/booth/booth-policy.tgz

EXPOSE 8080
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
