# cordis-python task runner. Every recipe goes through uv.

default: check

# Install/refresh the dev environment.
sync:
    uv sync --all-extras

# Lint and type gate. This is what CI enforces and what must be clean before
# any capability task is closed.
check: lint types

lint:
    uv run ruff check .
    uv run ruff format --check .

fix:
    uv run ruff check --fix .
    uv run ruff format .

types:
    uv run mypy

# Fast feedback: local-tier property tests only.
test:
    HYPOTHESIS_PROFILE=local uv run pytest -m "not tier_nightly and not tier_release"

# What a pull request runs.
test-pr:
    HYPOTHESIS_PROFILE=pr uv run pytest -m "not tier_release"

# Longer campaigns. Not part of the fast loop.
test-nightly:
    HYPOTHESIS_PROFILE=nightly uv run pytest

# Exhaustive: before a release, or when reproducing an incident.
test-release:
    HYPOTHESIS_PROFILE=release uv run pytest

# Replay a specific shrunk failure recorded in the example database.
replay TEST:
    HYPOTHESIS_PROFILE=local uv run pytest "{{TEST}}" -x

# Generator health, worst first. Read this as a trend, not a filter rate:
# Hypothesis counts its own overruns and duplicate draws as "invalid", so a
# strategy containing no filter at all still reports 10-20%. A number well
# above its neighbours is the signal worth chasing.
discards:
    #!/usr/bin/env bash
    set -euo pipefail
    HYPOTHESIS_PROFILE=pr uv run pytest -q --hypothesis-show-statistics \
      | awk '/^tests\/.*::/ {name=$0} /passing,.*invalid/ {
            inv=$7; pass=$2; total=pass+inv;
            printf "%5.1f%%  %4d invalid / %4d passing  %s\n",
                   (total ? 100*inv/total : 0), inv, pass, name }' \
      | sort -rn

# Validate the capability catalog: per-file schema, then cross-file
# consistency (dependency resolution, tier ordering, evidence references,
# property-card coverage of every MUST).
#
# The schema is JSON Schema 2020-12 written in YAML, validated with
# `check-jsonschema` so this recipe and CI run the same command on the same
# tool. `ys -f spec/schema/capability.v1.yaml <file>` checks one file against
# the same schema when that is closer to hand.
spec-check:
    #!/usr/bin/env bash
    set -euo pipefail
    uvx check-jsonschema --schemafile spec/schema/capability.v1.yaml spec/capabilities/*.yaml
    uv run spec/check_spec.py

# Print the capability catalog as a build-order table.
spec-table:
    #!/usr/bin/env bash
    set -euo pipefail
    printf '%-24s %4s %5s %6s  %s\n' capability tier rules props depends_on
    for f in spec/capabilities/*.yaml; do
        yq -r '.id + "|" + (.tier|tostring) + "|" + (.semantics|length|tostring) + "|" + (.properties|length|tostring) + "|" + ((.depends_on // [])|join(","))' "$f"
    done | awk -F'|' '{printf "%-24s %4s %5s %6s  %s\n",$1,$2,$3,$4,$5}'

# Regenerate the API reference from the docstrings and the capability records.
docs:
    uv run docs/build_reference.py

# Fail if the checked-in reference no longer matches the code. Part of CI so a
# renamed export cannot leave the documentation describing the old name.
docs-check:
    uv run docs/build_reference.py --check

# Everything CI runs, in CI's order.
ci: spec-check check docs-check test-pr
