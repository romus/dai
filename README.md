# dai

Two CLI coding agents argue about your task until they agree.

You give `dai` a prompt saying what to do. The **solver** does the work in your
current directory; the **critic**, running on a different engine, reviews it and
pushes back. The solver answers — fixing what it accepts, refusing what it
doesn't, with reasons. That repeats until they agree, or the rounds or budget
run out.

## Requirements

- `claude` and `codex` on your `PATH`, both logged in
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/)

## Quick start

```bash
make install        # create the venv, install deps
make doctor         # check uv / claude / codex are actually installed
make init           # write ~/.config/dai/config.toml (annotated)
make test           # 165 tests

make run ARGS="'fill in the empty cells in docs/matrix.md from README.md'"
```

`make help` lists every target.

## Make targets

| | |
|---|---|
| **Development** | |
| `make install` | install dependencies via uv |
| `make test` | run the tests, verbose |
| `make test-quick` | run the tests, quiet |
| `make doctor` | check the CLIs dai drives are installed |
| `make smoke` | end-to-end check in a throwaway sandbox (spends tokens) |
| **Run** | |
| `make run` | run in the current directory (see `ARGS` below) |
| `make run-here` | run against another directory (`DIR=…`) |
| `make run-no-tui` | plain streaming output, for scripts and CI |
| `make run-dry` | both agents read-only — nothing is written |
| `make run-swap` | codex solves, claude critiques |
| `make init` | write the default config |
| `make runs` | list past runs recorded here |
| `make snapshots` | show the git snapshots dai took in this repo |
| **Build & install** | |
| `make build` | wheel + sdist into `dist/` |
| `make install-cli` | install the `dai` command globally (uv) |
| `make uninstall-cli` | remove it again |
| `make pipx-install` | install globally via pipx instead |
| **Cleanup** | |
| `make clean` | build artifacts and caches |
| `make clean-runs` | recorded transcripts (leaves git snapshots alone) |
| `make clean-all` | everything, including `.venv` |

### Passing the task

Every `run` target takes `ARGS`. Quote the task itself, so it stays one argument:

```bash
make run         ARGS="'add the tests missing for the retry logic in client.py'"
make run         ARGS="'refactor the config loader' --rounds 3 --budget 2"
make run-dry     ARGS="'what would you change about the logging setup?'"
make run-no-tui  ARGS="'regenerate the CLI reference in docs/'"
make run         ARGS="-f task.md"
```

## Trying it on another directory

`dai` works on the directory you point it at, not the one it lives in.

**Quickest check — a throwaway sandbox:**

```bash
make smoke
```

That builds a small git repo in `/tmp/dai-smoke` holding a half-filled table,
runs a real argument in it, then prints the result, the repo's git state (HEAD
must be unmoved, only the edited file modified) and the snapshots taken. It
makes real model calls, so it costs a little. Point it elsewhere with
`make smoke SMOKE_DIR=/tmp/somewhere`.

**Against a real project, from this checkout:**

```bash
make run-here DIR=~/projects/foo ARGS="'bring the README in line with the code'"
uv run dai -C ~/projects/foo "bring the README in line with the code"   # same thing
```

**Against a real project, after installing:**

```bash
make install-cli
cd ~/projects/foo && dai "bring the README in line with the code"
```

In a repository you care about, start with `--dry-run`: both agents go
read-only, nothing is written, and you get their proposals instead.

## Installing the command

```bash
make install-cli
```

Then `dai` works from any directory — the directory you run it in is the context
the agents get, so there is no `ARGS` quoting to worry about:

```bash
dai "fill in the empty cells in docs/matrix.md using README.md as the source"
dai --rounds 3 --budget 2 "tidy up the error handling in api/"
dai --dry-run "what would you change about the logging setup?"
dai --lang <language> "pon la documentación al día con el código"
dai --theme light "tidy up the docstrings in src/"
dai --solver codex --critic claude "refactor the config loader"
dai --no-tui "regenerate the CLI reference in docs/"
dai --runs
dai --show 20260814-164131-1c4x
```

Exit codes: `0` agreed · `1` did not agree · `2` error.

## TUI keys

Solver on the left, critic on the right, verdicts along the bottom.

| key | |
|---|---|
| `q` | kill the agents and quit — asks first; after the run, just closes |
| `p` | pause / resume |
| `i` | say something to the agents — it outranks both |
| `a` | accept the work as it stands and stop |

On a deadlock the run pauses and asks who prevails.

### Light and dark

`dai` asks the terminal what colour it is and wears the matching palette. If you
change your terminal's theme mid-run it follows, scrollback included — on
terminals that report the change (Ghostty, kitty, WezTerm, foot, recent iTerm2,
Windows Terminal); elsewhere the palette is settled at startup. `--theme
dark|light` pins one, or `[tui] theme` in the config.

### Writing the task

The task box and the `i` box take more than one line. `Enter` sends;
`Shift+Enter` breaks the line.

`Shift+Enter` only arrives in terminals that speak the kitty keyboard protocol
— Ghostty, kitty, WezTerm, recent iTerm2. Everywhere else (Terminal.app among
them) use `Ctrl+J` or `Alt+Enter`, which do the same thing.

### Picking paths with `@`

In the task box and the `i` box, `@` opens a file picker scoped to the directory
the agents work in — so anything you can pick is something they can see.

A bare `@` lists the top level; typing after it fuzzy-matches the whole tree
(`@cmpltn` finds `src/dai/tui/completion.py`). While the list is open `↑`/`↓`
move through it, `Enter` or `Tab` picks, `Esc` closes it — with it closed those
keys go back to the text. Choosing a directory steps into it; choosing a
file inserts a plain relative path — the `@` never reaches the agents, since
codex has no `@`-syntax and both sides must get identical text.

Files ignored by git stay out of the list; brand-new untracked ones do not.

## Config

`~/.config/dai/config.toml`, created by `make init`, every option commented.
Defaults: `claude` solves, `codex` critiques, 5 rounds, $5.00, deadlock goes to
the critic, snapshots on.

```toml
[tui]
theme = "auto"                # "auto" follows the terminal; or pin "dark"/"light"
completion_debounce_ms = 80   # delay before the @ list refilters; 0 disables it
```

## Your git repo is not touched

Before each round, every git repository found is snapshotted to its own ref —
not a commit on your branch. HEAD, your branch, index and working tree are left
exactly as they were. Directories without git are skipped.

```bash
make snapshots                                          # list them
git diff refs/dai/<run-id>/r1 refs/dai/<run-id>/r2      # what changed
git restore --source refs/dai/<run-id>/r1 -- .          # go back to it
```

`make clean-runs` deletes transcripts but leaves these snapshots intact, so you
can still roll back after it.

## Notes

- **Codex reports tokens, not cost.** Its spend shows as *unmeasured* rather
  than as zero; add `[pricing.codex]` to the config for an estimate.
- **The claude critic runs read-only (`plan` mode)** and so cannot run your
  tests; the codex critic in `read-only` sandbox can. See `critic_args` in the
  config to change that.
- **Agents read your files** — use `make run-dry` in repositories you don't trust.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 romus.
