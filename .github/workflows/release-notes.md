---
description: Writes the release notes for a draft release of this image.
on:
  workflow_call:
    inputs:
      tag:
        description: Release tag to write notes for (e.g. v0.2.0)
        type: string
        required: true
  workflow_dispatch:
    inputs:
      tag:
        description: Release tag to write notes for (e.g. v0.2.0)
        type: string
        required: true
permissions:
  contents: read
  pull-requests: read
  copilot-requests: write
engine: copilot
concurrency:
  job-discriminator: ${{ inputs.tag }}
checkout:
  fetch-depth: 0
  fetch: ["refs/tags/*"]
tools:
  github:
    toolsets: [repos, pull_requests]
  bash: ["git log", "git describe", "git tag", "git show", "git diff", "git rev-list"]
safe-outputs:
  update-release:
    max: 1
---

# Release notes

Write the release notes for tag `${{ inputs.tag }}` of this repository
(`${{ github.repository }}`), a Python external-dns webhook provider for
DigitalOcean whose container image is published to
`ghcr.io/devinsba/external-dns-digitalocean`.

The draft release for the tag already exists. Replace its body with your notes
using the `update_release` safe output (operation `replace`, tag
`${{ inputs.tag }}`).

## Steps

1. Find the previous release tag: list `v*` tags with `git tag --list 'v*' --sort=-v:refname`
   (semver order) and take the one just below `${{ inputs.tag }}`. If there is none, this is
   the first release.
2. Gather what changed in `<prev>..${{ inputs.tag }}` (the whole history for a first release):
   `git log`, plus the merged pull requests behind those commits via the GitHub tools.
   Read PR descriptions and diffs (`git show`, `git diff`) whenever a title is unclear.
3. Write the notes for users and operators of the image, not for contributors.

## Format

Plain, specific, no marketing tone. Do not invent changes or mention anything you did
not see in the history. Use this shape, in this order:

1. One paragraph summarizing the release.
2. The pull command in a code block, with the version without its leading `v`:
   ```
   ghcr.io/devinsba/external-dns-digitalocean:<version>
   ```
3. `## Highlights`: user-visible changes. Leave out CI, chore, and dependency-only noise.
4. `## Breaking changes / upgrade notes`: only if there are any (config or env var, port,
   or behavior changes). Say exactly what the operator must change.
5. `## Fixes`: only if there are any.
6. `## What's Changed`: one line per merged PR, `* <title> by @<author> in <url>`.
7. `**Full Changelog**: https://github.com/devinsba/external-dns-digitalocean/compare/<prev>...${{ inputs.tag }}`
   (for the first release use `https://github.com/devinsba/external-dns-digitalocean/commits/${{ inputs.tag }}`).

Omit any optional section that would be empty. Do not add other sections.
