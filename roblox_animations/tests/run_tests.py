"""Run the add-on's unittest suite inside Blender (background mode).

Usage: blender --background --factory-startup --online-mode --python roblox_animations/tests/run_tests.py -- [module ...] [-v]
Modules are names under roblox_animations.tests (e.g. test_serialization).
With no modules, every test_*.py is run.
"""
import os
import sys
import time
import traceback
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
verbosity = 1
if "-v" in argv:
    argv.remove("-v")
    verbosity = 2

import roblox_animations  # noqa: E402

try:
    roblox_animations.register()
except Exception:
    traceback.print_exc()

tests_dir = os.path.join(REPO, "roblox_animations", "tests")
if argv:
    names = [n[:-3] if n.endswith(".py") else n for n in argv]
else:
    # test_clothing_bake_smoke is a standalone script (it replaces
    # sys.modules["roblox_animations"]); run it on its own, never in-process.
    SCRIPTS = {"test_clothing_bake_smoke"}
    names = sorted(f[:-3] for f in os.listdir(tests_dir)
                   if f.startswith("test_") and f.endswith(".py") and f[:-3] not in SCRIPTS)

loader = unittest.defaultTestLoader
results = {}
total_start = time.time()
_baseline_modules = dict(sys.modules)
for name in names:
    # Restore the package if a previous test module replaced it.
    for key in [k for k in sys.modules if k.startswith("roblox_animations") and sys.modules[k] is not _baseline_modules.get(k)]:
        if key in _baseline_modules:
            sys.modules[key] = _baseline_modules[key]
    full = f"roblox_animations.tests.{name}"
    start = time.time()
    try:
        suite = loader.loadTestsFromName(full)
        # Also collect pytest-style module-level test functions.
        module = sys.modules.get(full)
        if module is not None:
            import inspect
            for attr, value in sorted(vars(module).items()):
                if attr.startswith("test_") and inspect.isfunction(value) and value.__module__ == full:
                    if not inspect.signature(value).parameters:
                        suite.addTest(unittest.FunctionTestCase(value, description=f"{name}.{attr}"))
    except Exception:
        print(f"=== {name}: IMPORT ERROR")
        traceback.print_exc()
        results[name] = ("import-error", 0, 0, 0, 0.0)
        continue
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=verbosity)
    res = runner.run(suite)
    elapsed = time.time() - start
    results[name] = ("ok" if res.wasSuccessful() else "FAIL", res.testsRun, len(res.failures), len(res.errors), elapsed)
    print(f"=== {name}: run={res.testsRun} fail={len(res.failures)} err={len(res.errors)} skip={len(res.skipped)} ({elapsed:.1f}s)")

print("\n==== SUMMARY ====")
tot_run = tot_fail = tot_err = 0
for name, (status, run, fail, err, elapsed) in results.items():
    tot_run += run; tot_fail += fail; tot_err += err
    print(f"{status:12s} {name:45s} run={run:4d} fail={fail:3d} err={err:3d} {elapsed:6.1f}s")
print(f"TOTAL run={tot_run} fail={tot_fail} err={tot_err} in {time.time()-total_start:.1f}s")
sys.stdout.flush()
