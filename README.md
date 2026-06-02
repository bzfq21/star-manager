# star-manager

Automated GitHub Stars classification for `bzfq21`.

This repository contains the private operating code for sorting starred repositories into GitHub Lists. It was split out from `bzfq21/bzfq21` so the personal homepage repository can stay focused on public presentation.

## Files

- `scripts/star_manager.py` - Fetches starred repositories, classifies new stars, and updates GitHub Lists.
- `scripts/rules.json` - Keyword/category rules.
- `scripts/state.json` - Last processed star state, updated by the workflow.
- `.github/workflows/organize-stars.yml` - Weekly/manual GitHub Actions workflow.

## Required secret

Configure this repository secret before running the workflow:

```text
STAR_MANAGER_PAT
```

The token must be able to read starred repositories, update GitHub Lists, and update `scripts/state.json` in this repository.

## Run

The workflow runs weekly and can also be started manually from GitHub Actions. Use `force_reclassify` only when rules change and all stars should be reassigned.
