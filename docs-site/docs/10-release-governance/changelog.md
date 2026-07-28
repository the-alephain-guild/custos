---
title: "Changelog"
sidebar_position: 2
---

# Changelog

The changelog lives in the repository, where it sits next to the code it
describes and is updated in the same commit:

**[CHANGELOG.md](https://github.com/the-alephain-guild/custos/blob/main/CHANGELOG.md)**

It follows [Keep a Changelog](https://keepachangelog.com/) and the versioning
contract in [SemVer and LTS](/release-governance/semver-lts). Every entry is
filed under the bump that carried it, so the section a change appears in tells
you whether upgrading to it requires action.

## Reading an entry

`### Removed` and `### Changed` are the ones that cost you time. Both appear
only in a MAJOR or, for a documented removal, a MINOR release, and both point at
the migration steps in [upgrade paths](/release-governance/upgrade-paths).

`### Deprecated` is the early warning. Anything listed there stays available
for at least the following minor release, and is repeated in every release
notice until it is actually removed — so a deprecation cannot pass unnoticed
between the announcement and the removal.

## Current release

**0.3.0**, released 2026-07-12. Its remote artifacts are deferred: there is no
published wheel or pullable image for this version, and the consumer gate is a
locally built and verified image. See [installation](/getting-started/installation).
