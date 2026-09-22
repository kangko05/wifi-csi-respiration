# Repository Guidelines

## Project Structure

- `src/` and `include/csi_resp/`: C11 preprocessing, filters, amplitude and phase estimation, and public headers. `src/main.c` provides the host executable.
- `csi-rx/` and `csi-tx/`: separate ESP-IDF firmware projects for ESP32-C5.
- `tests/`: Python-driven C reference tests and C bridges. `python/tests/`: collector, adapter, and evaluation tests.
- `python/`: collection scripts, analysis tools, and preserved legacy code under `vendor/`.
- `data/`: immutable captures; `outputs/` and `python/outputs/`: derived results. Keep these separate.
- `docs/`, `python/docs/`, and `python/PROGRESS.md`: implementation notes and experiment history. Reference PDFs live at `~/Documents/csi-respiration`.

## Build, Test, and Development Commands

Run host commands from the repository root:

```bash
cmake -S . -B build -G Ninja
cmake --build build
./build/csi_resp_main
```

These configure and compile the host library/executable, then run a synthetic 15-bpm demonstration. Use a fresh build directory when switching operating systems. Firmware requires a configured ESP-IDF environment and a separate build in its project directory.

Set up Python dependencies and run both suites:

```bash
python3 -m venv python/.venv
python/.venv/bin/python -m pip install -r python/requirements.txt
python/.venv/bin/python -m unittest discover -s tests -v
(cd python && .venv/bin/python -m unittest discover -s tests -v)
```

## Coding Style

Follow adjacent code: four-space indentation, snake_case functions and variables, uppercase constants, and `csi_` prefixes for public C APIs. Keep signal processing separate from collection and reporting. No repository-wide formatter or linter is configured; `.clangd` uses the host CMake compilation database.

## Testing Guidelines

Use `unittest`, `test_*.py` files, and `test_*` methods. C reference tests require GCC, NumPy, and SciPy. Cover numerical parity, timestamps, masks, gaps, and invalid inputs. No coverage percentage is mandated. Report failures explicitly; existing records identify 14 Python input-validation failures. Synthetic tests do not establish respiration accuracy.

## Commits and Pull Requests

History uses short descriptive messages, such as `phase config added`; no enforced commit prefix exists. Keep changes focused. PRs should explain behavior, affected paths, validation commands/results, and remaining limitations. Commit or push only when explicitly requested.

## Data and Agent Constraints

Never overwrite captures or modify vendored reference code. Use manual breath counts only for post-estimation evaluation. Confirm hardware readiness before opening ports or flashing. Read `python/AGENTS.md` before Python work; it contains additional preservation and implementation-delegation instructions.
