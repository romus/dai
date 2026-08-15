# dai

Two CLI coding agents argue about your task until they agree.

The **solver** does the work in your current directory; the **critic**, on a different
engine, reviews it and pushes back. The solver answers — fixing what it accepts,
refusing what it doesn't, with reasons — until they agree or the rounds or budget
run out.

![The task box with the @ picker open, offering index.html, and the key hints underneath](docs/screenshot-task.png)

![The dai TUI mid-round: solver on the left streaming its tool calls, critic on the right waiting, round and budget in the header](docs/screenshot-debate.png)

## Requirements

- `claude` and `codex` on your `PATH`, both logged in
- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/)

## Quick start

```bash
make install        # create the venv, install deps
make doctor         # check uv / claude / codex are actually installed
make init           # write ~/.config/dai/config.toml (annotated)
make test           # 327 tests

make run ARGS="'fill in the empty cells in docs/matrix.md from README.md'"
```

`make help` lists every target.

## Running it

`dai` works on the directory you point it at, not the one it lives in. Install the
command once and the directory you run it in is the context the agents get:

```bash
make install-cli
cd ~/projects/foo && dai "bring the README in line with the code"
```

```bash
dai --rounds 3 --budget 2 "tidy up the error handling in api/"
dai --dry-run "what would you change about the logging setup?"
dai --lang <language> "pon la documentación al día con el código"
dai --solver codex --critic claude "refactor the config loader"
dai --no-tui "regenerate the CLI reference in docs/"
dai --no-merge "bring the changelog up to date"   # leave it on the run's branch
dai --branch-from current "tidy up the tests"     # branch off where I am, not the trunk
dai --runs                                        # past runs; --show <id> prints one
dai --snapshots                                   # branches those runs committed to
```

From this checkout instead: `make run-here DIR=~/projects/foo ARGS="'the task'"` or
`uv run dai -C ~/projects/foo "the task"` — every `run` target takes `ARGS`, quoted so
the task stays one argument.

In a repository you care about, start with `--dry-run`: both agents go read-only and
you get their proposals instead. `make smoke` runs a real argument in a throwaway
sandbox — real model calls, so it costs a little.

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

The task box and the `i` box take more than one line: `Enter` sends, `Shift+Enter`
breaks the line — or `Ctrl+J` / `Alt+Enter` in terminals that don't speak the kitty
keyboard protocol (Terminal.app among them).

In both boxes `@` opens a file picker scoped to the agents' directory. A bare `@` lists
the top level; typing after it fuzzy-matches the whole tree (`@cmpltn` finds
`src/dai/tui/completion.py`). `↑`/`↓` move, `Enter` or `Tab` picks, `Esc` closes. What
lands in the text is a plain relative path — the `@` never reaches the agents.

`dai` asks the terminal what colour it is and wears the matching palette, following a
mid-run theme change on terminals that report one. `--theme dark|light` pins one.

## Config

`~/.config/dai/config.toml`, created by `make init`, every option commented. Defaults:
`claude` solves, `codex` critiques, 5 rounds, $5.00, deadlock goes to the critic,
per-round commits on, merged back into your branch on agreement.

```toml
[tui]
theme = "auto"                # "auto" follows the terminal; or pin "dark"/"light"
completion_debounce_ms = 80   # delay before the @ list refilters; 0 disables it

[snapshot]
branch_from = "default"       # the trunk; or "current", or a branch name
merge = true                  # on agreement, fast-forward that branch onto the work
```

## Branches and commits

A repository the agents change is moved onto `dai/<run-id>`, rooted on whatever it
would merge back into — `main`/`master` by default. Each round lands there as an
ordinary commit, and at consensus that base branch is fast-forwarded onto the result,
leaving you on your own branch with the work committed and `git status` clean. Only
consensus merges: a deadlock, a run out of budget or one you killed leaves you on
`dai/<run-id>` instead.

**Repositories nothing changed in are not touched at all.** Whatever was uncommitted
before you started is kept as a `baseline` commit of its own. Not one file on disk is
ever rewritten by dai and none of your hooks fire — which is also why a repository
with anything *staged* is left alone, and says so.

The directory you run in need not be the repository, so the run tells you *which* one
ended up with what:

```bash
dai --snapshots                               # or: make snapshots
git -C <repo> log --oneline <base>..<branch>  # the rounds
git -C <repo> diff <base> <branch>            # everything they changed
git -C <repo> reset --hard <base>             # undo the lot
git -C <repo> branch -D dai/<run-id>          # forget the run
```

`<base>` is printed in the report, and is used rather than `HEAD` because `HEAD` stops
being the right answer the moment anything moves.

## Notes

- **Codex reports tokens, not cost.** Its spend shows as *unmeasured* rather
  than as zero; add `[pricing.codex]` to the config for an estimate.
- **The claude critic runs read-only (`plan` mode)** and so cannot run your
  tests; the codex critic in `read-only` sandbox can. See `critic_args` in the
  config to change that.
- **Agents read your files** — use `--dry-run` in repositories you don't trust.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 romus.
