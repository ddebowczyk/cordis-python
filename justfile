# cordis-python entry point.
#
# Nothing is defined here. Every operation belongs to a capability under
# `ops/`, which owns its own scripts, schemas, documentation and recipes; this
# file is the door. `just ops <capability> <command>` runs anything in the
# catalogue, and the short names below are aliases for the ones reached for
# most often.
#
#   just list                the catalogue
#   just ops docs build      any capability command
#   just check               every gate that declares itself part of `check`
#
# ops/README.md explains what a capability is and how ownership is enforced.

default: check

# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

# Run any command of any capability: `just ops release status`.
ops capability command *args:
    @just --justfile ops/{{ capability }}/justfile --working-directory . {{ command }} {{ args }}

# What capabilities exist, what they provide, and what they run.
list:
    @just ops control list

# Hold every manifest to what it claims.
validate:
    @just ops control validate

# --------------------------------------------------------------------------
# Repository-wide lanes
#
# Assembled from the manifests: a command joins a lane by declaring
# `aggregate: check` (or `aggregate: test`) in its own capability. There is no
# list here that can fall out of step with the capabilities it covers.
# --------------------------------------------------------------------------

check:
    @just ops control aggregate check

check-tests:
    @just ops control aggregate test

# --------------------------------------------------------------------------
# Aliases
# --------------------------------------------------------------------------

sync:
    @just ops workflow sync

doctor:
    @just ops workflow doctor

ci:
    @just ops workflow ci

lint:
    @just ops quality lint

types:
    @just ops quality types

fix:
    @just ops quality fix

test:
    @just ops test fast

test-pr:
    @just ops test pr

test-nightly:
    @just ops test nightly

test-release:
    @just ops test release

replay selector:
    @just ops test replay "{{ selector }}"

discards:
    @just ops test discards

spec-check:
    @just ops spec check

spec-table:
    @just ops spec table

docs:
    @just ops docs build

docs-check:
    @just ops docs check
