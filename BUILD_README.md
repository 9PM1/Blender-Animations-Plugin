# Building the Blender Addon

This project includes build scripts to package the addon without the tests directory.

## Build Script

### Batch File
```batch
# Build with auto-detected version from __init__.py
build_addon.bat

# Build with custom version
build_addon.bat v2.2.0
```

## Output

The scripts will create a zip file named `rbx_anim_v[major].[minor].[patch].zip` (e.g., `rbx_anim_v2.1.1.zip`) containing the addon without the tests directory.

## What gets excluded

- `roblox_animations/tests/` directory
- All dot directories (`.git/`, `.vscode/`, `.ruff_cache/`, etc.)
- `__pycache__/` directories
- Common cache directories (`.ruff_cache/`, `.pytest_cache/`, `.mypy_cache/`)
- `*.pyc`, `*.pyo`, `*.pyd` files

## Version Detection

If no version is specified, the script automatically extracts the version from `roblox_animations/__init__.py` in the `bl_info` dictionary.

## Building the Studio plugin

The plugin uses Fusion 0.2, pinned in `wally.toml`:

```
wally install
rojo build default.project.json -o RBXMonkey.rbxmx
```

`default.project.json` leaves out the specs, the vendored TestEZ and the
TestRunner script. Use `test.project.json` to build a place that includes them
for running the TestEZ specs in Studio.

## Running the tests

Blender add-on (needs Blender 4.2+; `--online-mode` lets the network tests run,
they never reach the network):

```
blender --background --factory-startup --online-mode --python roblox_animations/tests/run_tests.py
blender --background --factory-startup --online-mode --python roblox_animations/tests/run_tests.py -- test_ledger_fixes -v
```

Studio plugin modules, outside Studio (needs [Lune](https://lune-org.github.io/docs)):

```
lune run tests-lune/run
lune run tests-lune/compile_all
```

End to end (real Blender server, real Studio connection code under Lune):

```
blender --background --factory-startup --online-mode --python tests-lune/e2e_blender_server.py -- 38417 e2e_stop
lune run tests-lune/e2e 38417 __TemplateR6_Armature
```

Create the `e2e_stop` file to shut the Blender side down.
