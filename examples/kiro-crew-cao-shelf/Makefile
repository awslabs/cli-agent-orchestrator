# CAO Shelf: checks, build, dev harness and e2e. Run from examples/kiro-crew-cao-shelf/.
#
#   make check-backend     ruff, black, isort, mypy (when installed) and the backend tests
#   make check-ui          typecheck, lint, component tests, build and the bundle guards (needs `make ui-deps` once)
#   make check-fixtures    the committed fixtures equal the generator output and validate against schemas/
#   make check-rig         the rig's own tests (fake CAO, install.sh, manifest) and shellcheck
#   make check             all four
#   make build-ui          build ui/bundle/index.mjs      make ui-deps   npm ci in ui/
#   make bundle-drift      rebuild and fail when ui/bundle differs from git (CI)
#   make fixtures          regenerate fixtures in place (a shared-contract change: schemas/ and api-contract.md move with it)
#   make dev               look at the page without Kiro Crew (fake CAO + real backend + SDK stub), port 8765
#   make harness-up / harness-down / harness-status   isolated Kiro Crew (5499) + scratch CAO (9990) + the App
#   make e2e               Playwright journeys against the harness (starts and stops it unless SHELF_E2E_KEEP=1)
#   make install-isolated  ./install.sh into the ISOLATED home only (never your real Kiro Crew)
#   make real-install      ./install.sh into YOUR real Kiro Crew; needs REAL_KIROCREW=yes (see README)
#
# Variables: SHELF_SCRATCH (scratch root, default ~/kc-shelf-e2e; never /tmp), PY (python), PLAYWRIGHT_BROWSERS_PATH.

SHELL := /bin/bash
HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
SHELF_SCRATCH ?= $(HOME)/kc-shelf-e2e
export SHELF_SCRATCH
PY ?= python3
PYTEST ?= $(PY) -m pytest -p no:cacheprovider
# the repository's pytest addopts (coverage on src/) do not apply to this folder when run alone
PYTEST_FLAGS ?= -o addopts= -q
PW_PY ?= $(PY)
RIG := e2e/rig

.PHONY: help check check-backend check-ui check-fixtures check-rig lint-sh build-ui ui-deps bundle-drift \
        fixtures dev harness-up harness-down harness-status e2e install-isolated real-install

help:
	@sed -n '2,/^$$/p' $(firstword $(MAKEFILE_LIST)) | sed 's/^# \{0,1\}//'

check: check-backend check-ui check-fixtures check-rig

check-backend:
	cd $(HERE) && $(PY) -m ruff check backend tests
	cd $(HERE) && $(PY) -m black --check -l 100 backend tests
	cd $(HERE) && $(PY) -m isort --check-only --profile black -l 100 backend tests
	@if $(PY) -c "import mypy" 2>/dev/null; then cd $(HERE) && $(PY) -m mypy --strict backend; else echo "check-backend: mypy not installed, skipped"; fi
	cd $(HERE) && $(PYTEST) $(PYTEST_FLAGS) tests

check-ui:
	cd $(HERE)ui && npm run check

build-ui:
	cd $(HERE)ui && npm run build

ui-deps:
	cd $(HERE)ui && npm ci

bundle-drift: build-ui
	cd $(HERE) && git diff --exit-code -- ui/bundle

check-fixtures:
	cd $(HERE) && $(PY) $(RIG)/check_fixtures.py

fixtures:
	cd $(HERE) && $(PY) fixtures/_gen/make_fixtures.py

check-rig: lint-sh
	cd $(HERE) && $(PYTEST) $(PYTEST_FLAGS) $(RIG) --ignore=$(RIG)/dev_host

lint-sh:
	cd $(HERE) && shellcheck -x -P SCRIPTDIR install.sh $(RIG)/*.sh

dev:
	cd $(HERE) && $(PY) $(RIG)/dev_server.py $(DEV_ARGS)

harness-up:
	cd $(HERE) && $(RIG)/harness.sh up $(HARNESS_ARGS)

harness-down:
	cd $(HERE) && $(RIG)/harness.sh down

harness-status:
	cd $(HERE) && $(RIG)/harness.sh status

e2e:
	cd $(HERE) && $(PW_PY) -m pytest -c e2e/pytest.ini e2e/tests -m e2e -q

install-isolated:
	cd $(HERE) && $(RIG)/kc.sh ./install.sh --trust-app

real-install:
	@if [ "$(REAL_KIROCREW)" != "yes" ]; then \
	  echo "real-install installs into YOUR real Kiro Crew (HOME=$$HOME). Nothing ran."; \
	  echo "Read install.sh, then: make real-install REAL_KIROCREW=yes"; exit 2; fi
	cd $(HERE) && ./install.sh --trust-app
