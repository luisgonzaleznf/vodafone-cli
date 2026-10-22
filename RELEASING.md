# Releasing

Releases are git tags `vX.Y.Z` on `main`. The version is set once, in `pyproject.toml`; `vodafone
--version` and the MCP server read it from the installed package metadata.

A release is pinned in three places:

| File | What | When it changes |
|---|---|---|
| `pyproject.toml` (and the `vodafone-cli` entry in `uv.lock`) | `version` | in the release PR |
| `README.md` | the `@vX.Y.Z` of the `uvx` (MCP) and `uv tool install` lines | in the release PR; the tag follows the merge |
| `Formula/vodafone.rb` | `tag:` and `revision:` | after the tag exists |
| `install.sh` | `DEFAULT_VERSION` | after the tag exists, with the formula |

`tests/test_packaging.py` fails if the formula and `install.sh` point at different releases, or if
the README's lines pin another release than `pyproject.toml`.

## 1. Release PR

On a branch:

```bash
# bump `version` in pyproject.toml and the README's @vX.Y.Z pins, then refresh the project's own
# entry in the lockfile
uv lock
uv run pytest
shellcheck install.sh
```

Check that the build contains only the package, `LICENSE`, `README.md` and metadata (no `docs/`,
`verify_phase0.py`, `.local-secrets/`, `*.har`, `*.pdf`; `pyproject.toml.orig` is the build
backend's copy of the original `pyproject.toml`):

```bash
out=$(mktemp -d) && uv build -o "$out" && tar tzf "$out"/*.tar.gz && unzip -l "$out"/*.whl
```

Merge the PR into `main`.

## 2. Tag

```bash
git fetch origin
git tag -a vX.Y.Z -m "vX.Y.Z" origin/main
git push origin vX.Y.Z
```

Optionally create a GitHub release from the tag (`gh release create vX.Y.Z --generate-notes`).

## 3. Point the formula and the install script at the tag

The formula has to name the tagged commit, and a commit cannot contain its own hash, so this is a
follow-up commit on `main` (directly or through a small PR):

- `Formula/vodafone.rb`: `tag: "vX.Y.Z"` and `revision: "<full commit hash>"`, from
  `git rev-list -n 1 vX.Y.Z`:

  ```ruby
  url "https://github.com/luisgonzaleznf/vodafone-cli.git",
      tag:      "vX.Y.Z",
      revision: "0123456789abcdef0123456789abcdef01234567"
  ```

- `install.sh`: `DEFAULT_VERSION=X.Y.Z`
- `uv run pytest tests/test_packaging.py`

Until this lands, Homebrew and the install script keep installing the previous release. Homebrew
users get the new formula with `brew update`, because a tap follows `main`.

## 4. Verify Homebrew

```bash
brew tap luisgonzaleznf/vodafone-cli https://github.com/luisgonzaleznf/vodafone-cli   # or: brew update
brew audit --strict --online luisgonzaleznf/vodafone-cli/vodafone
brew style luisgonzaleznf/vodafone-cli/vodafone
brew install luisgonzaleznf/vodafone-cli/vodafone
brew test luisgonzaleznf/vodafone-cli/vodafone
"$(brew --prefix)/bin/vodafone" --version
```

Use `brew upgrade luisgonzaleznf/vodafone-cli/vodafone` when an older version is installed. Without a `revision:`, `brew audit --strict` reports "Git should specify a
full commit hash". `brew install` also upgrades outdated dependencies (`python@3.14`, …), as any
install does. `"$(brew --prefix)/bin/vodafone"` is spelled out because an editable development
install in `~/.local/bin` usually comes first on `PATH`.

## 5. Verify the install script

Point uv's tool directories at a temporary directory so an existing install is not touched:

```bash
d=$(mktemp -d)
curl -fsSL https://raw.githubusercontent.com/luisgonzaleznf/vodafone-cli/main/install.sh \
  | UV_TOOL_DIR="$d/tools" UV_TOOL_BIN_DIR="$d/bin" sh
"$d/bin/vodafone" --version
```

## Optional: a release tarball

- Switch the formula from the git URL to the release tarball, which downloads faster and
  needs no git. Step 3 then updates `url` and `sha256` instead of `tag:` and `revision:`:

  ```ruby
  url "https://github.com/luisgonzaleznf/vodafone-cli/archive/refs/tags/vX.Y.Z.tar.gz"
  sha256 "<shasum -a 256 of that file>"
  ```

## PyPI (not published yet)

The metadata is ready and `uv build` produces a clean sdist and wheel. At the time of writing both
`vodafone-cli` (the distribution name) and `vodafone` (the import package and command name) are
unregistered on PyPI; this project only needs `vodafone-cli`.

1. Create the project on PyPI, preferably with a
   [trusted publisher](https://docs.pypi.org/trusted-publishers/) (a GitHub Actions workflow) so
   no API token is stored anywhere; otherwise use a project-scoped token in `UV_PUBLISH_TOKEN`.
2. Try TestPyPI first: `uv build && uv publish --publish-url https://test.pypi.org/legacy/`.
3. Publish the tagged release: `uv build && uv publish`.
4. Users can then run `uv tool install vodafone-cli` or `pipx install vodafone-cli`. Consider
   pointing `install.sh` at PyPI (`vodafone-cli==$version`) and adding that to the README.
