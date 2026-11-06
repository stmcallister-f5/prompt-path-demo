# Booth demo: The Prompt Got Past Your WAF
NS          ?= prompt-demo
IMAGE       ?= prompt-path-demo:dev
CLUSTER     ?= prompt-demo
OVERLAY     ?= deploy/base
NGF_VERSION ?= v2.1.0
BOARD_PORT  ?= 8080

.PHONY: venv waf-image waf-load dev test image kind-up ngf deploy board watch reset down

# Local Python: a venv in .venv. Needs Python 3.10+ (macOS's built-in python3 is 3.9).
PYTHON      ?= $(firstword $(foreach v,3.13 3.12 3.14 3.11 3.10 3.15,$(shell command -v python$(v) 2>/dev/null)) $(shell command -v python3))
VENV        := .venv
VPY         := $(VENV)/bin/python

venv: $(VENV)/.installed   ## Create .venv and install the demo's dependencies
$(VENV)/.installed: app/requirements.txt
	@$(PYTHON) -c 'import sys; v=sys.version_info; sys.exit(0 if v >= (3,10) else "Python 3.10+ needed, found %d.%d at $(PYTHON). Install a newer one or run: make venv PYTHON=/path/to/python3.12" % v[:2])'
	$(PYTHON) -m venv $(VENV)
	$(VPY) -m pip install -q --upgrade pip
	$(VPY) -m pip install -q -r app/requirements.txt pytest
	@touch $@

dev: venv       ## Run the whole stack on this laptop (no cluster) at http://127.0.0.1:9100
	PATH="$(CURDIR)/$(VENV)/bin:$$PATH" scripts/dev.sh

test: venv      ## Walk the booth script against a running board (BOARD_URL=...)
	$(VPY) -m pytest -q tests/

image:          ## Build the single image every hop runs
	docker build -t $(IMAGE) .

WAF_IMAGE ?= f5-waf-nginx:5.16.1
# Directory holding your NGINX One subscription .jwt, .crt and .key (set it in your shell).
NGINX_LICENSE_DIR ?= $(error Set NGINX_LICENSE_DIR to the directory with your NGINX One .jwt, .crt and .key)
WAF_COMPILER ?= private-registry.nginx.com/nap/waf-compiler:5.16.1
waf-image:      ## Compile the booth WAF policy and build NGINX Plus + F5 WAF module (amd64; needs NGINX One cert/key in NGINX_LICENSE_DIR)
	@# Compile in /tmp: Docker Desktop may not be allowed to mount ~/Documents (macOS privacy).
	mkdir -p /tmp/f5-waf-policy && chmod 777 /tmp/f5-waf-policy
	cp deploy/images/f5-waf/booth-policy.json /tmp/f5-waf-policy/
	docker run --rm --platform linux/amd64 -v /tmp/f5-waf-policy:/work $(WAF_COMPILER) \
	  -p /work/booth-policy.json -o /work/booth-policy.tgz | tail -c 300
	cp /tmp/f5-waf-policy/booth-policy.tgz deploy/images/f5-waf/
	docker build --platform linux/amd64 -t $(WAF_IMAGE) \
	  --secret id=nginx-crt,src=$(wildcard $(NGINX_LICENSE_DIR)/*.crt) \
	  --secret id=nginx-key,src=$(wildcard $(NGINX_LICENSE_DIR)/*.key) \
	  deploy/images/f5-waf

waf-load:       ## Load the WAF images into kind (amd64 archives; the node can't pull from the registry itself)
	@for img in $(WAF_IMAGE) private-registry.nginx.com/nap/waf-enforcer:5.16.1 private-registry.nginx.com/nap/waf-config-mgr:5.16.1; do \
	  docker image inspect $$img >/dev/null 2>&1 || docker pull --platform linux/amd64 $$img; \
	  docker save --platform linux/amd64 -o /tmp/waf-img.tar $$img && kind load image-archive /tmp/waf-img.tar --name $(CLUSTER); \
	done; rm -f /tmp/waf-img.tar

kind-up: image  ## Local kind cluster with the image loaded (best for the booth: no Wi-Fi needed)
	kind create cluster --name $(CLUSTER) || true
	kind load docker-image $(IMAGE) --name $(CLUSTER)

ngf:            ## Install Gateway API CRDs and NGINX Gateway Fabric
	kubectl kustomize "https://github.com/nginx/nginx-gateway-fabric/config/crd/gateway-api/standard?ref=$(NGF_VERSION)" | kubectl apply -f -
	helm upgrade --install ngf oci://ghcr.io/nginx/charts/nginx-gateway-fabric --version $(NGF_VERSION:v%=%) \
	  --create-namespace -n nginx-gateway --wait

deploy:         ## Apply the stack (OVERLAY=deploy/overlays/f5-waf-guardrails for the F5 booth stack)
	kubectl apply -k $(OVERLAY)
	kubectl -n $(NS) rollout status deploy --timeout=180s

board:          ## Open the toggle board on http://localhost:$(BOARD_PORT)
	kubectl -n $(NS) port-forward svc/board $(BOARD_PORT):8080

watch:          ## Second screen: watch the slot Services flip as you toggle
	kubectl -n $(NS) get svc -l demo/slot=true -o custom-columns=SLOT:.metadata.name,SELECTS:.spec.selector.app\\.kubernetes\\.io/component -w

reset:          ## Every layer off, ready for the next session
	curl -fsS -X POST http://localhost:$(BOARD_PORT)/api/preset/all-off >/dev/null && echo "all layers off"

down:
	kind delete cluster --name $(CLUSTER)
