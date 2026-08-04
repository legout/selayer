# selayer-discovery

Deterministic agent-assisted semantic discovery for selayer.

The companion package currently provides a minimal executable scaffold:

```bash
uv run --package selayer-discovery selayer-discovery --help
```

## Development

Sync the full workspace so the member is installed:

```bash
uv sync --all-packages
```

Plain `uv sync` only installs the root `selayer` package and will uninstall
this member. Run the member tests with:

```bash
uv run pytest packages/selayer-discovery/tests -q
```
