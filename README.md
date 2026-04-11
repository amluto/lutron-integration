# What is this? #

This is the home of the [lutron-integration](packages/lutron-integration/README.md)
and [lutron-integration-tools](packages/lutron-integration-tools/README.md) packages.

If you want to write a library or integration that speaks the Lutron Integration
Protocol, check out lutron-integration.  If you want to use one of the tools that
live here (currently just lutron_monitor), check out lutron-integration-tools.

If you want to use Lutron QS Standalone via Home Assistant, check out the lutronqs
integration in Home Assistant (currently under development).

## Release workflow

This repository publishes two packages from the same tag:

- `lutron-integration`
- `lutron-integration-tools`

Trusted publishing is wired through [`.github/workflows/release.yml`](.github/workflows/release.yml).
That workflow runs when you push a tag matching `v*`, for example `v0.0.0a1`.

### Update versions

Use [`update-version.sh`](update-version.sh) to update both packages together.
It uses `uv version` for the package versions and also rewrites the exact dependency
from `lutron-integration-tools` to `lutron-integration`.

Set an explicit version:

```bash
./update-version.sh 0.0.0a2
```

Or ask `uv` to bump semantically:

```bash
./update-version.sh --bump patch
./update-version.sh --bump alpha
```

The script updates:

- `packages/lutron-integration/pyproject.toml`
- `packages/lutron-integration-tools/pyproject.toml`
- `uv.lock`

### Verify a release

Use [`verify-release.py`](verify-release.py) before tagging or publishing:

```bash
./verify-release.py 0.0.0a2
```

This script:

- runs `check.sh`
- verifies both packages have the same version
- verifies that the requested version matches the package version
- verifies that `lutron-integration-tools` depends on `lutron-integration==<that version>`

The GitHub release workflow uses this same script, passing the tag version with the
leading `v` removed.

### Create a release tag

Use [`tag-release.sh`](tag-release.sh) to create the git tag safely:

```bash
./tag-release.sh 0.0.0a2
```

This runs `verify-release.py 0.0.0a2` first and then creates the annotated tag
`v0.0.0a2`. After that, push the tag:

```bash
git push origin v0.0.0a2
```

Pushing the tag triggers the release workflow, which rebuilds the distributions,
reruns verification in the sandboxed CI step, and publishes both packages to PyPI.
