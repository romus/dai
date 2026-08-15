# dai

Two CLI coding agents argue about your task until they agree.

You give `dai` a prompt saying what to do. The **solver** does the work in your
current directory; the **critic**, running on a different engine, reviews it and
pushes back. The solver answers — fixing what it accepts, refusing what it
doesn't, with reasons. That repeats until they agree, or the rounds or budget
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
make test           # 315 tests

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
| `make snapshots` | show which repo dai committed its rounds to, and on what branch |
| **Build & install** | |
| `make build` | wheel + sdist into `dist/` |
| `make install-cli` | install the `dai` command globally (uv) |
| `make uninstall-cli` | remove it again |
| `make pipx-install` | install globally via pipx instead |
| **Cleanup** | |
| `make clean` | build artifacts and caches |
| `make clean-runs` | recorded transcripts (leaves dai's git branches alone) |
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
must be unmoved, only the edited file modified) and what dai committed. It
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
dai --no-merge "bring the changelog up to date"   # leave it on the run's branch
dai --branch-from current "tidy up the tests"    # branch off where I am, not the trunk
dai --runs
dai --show 20260814-164131-1c4x
dai --snapshots
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
the critic, per-round commits on, merged back into your branch on agreement.

```toml
[tui]
theme = "auto"                # "auto" follows the terminal; or pin "dark"/"light"
completion_debounce_ms = 80   # delay before the @ list refilters; 0 disables it

[snapshot]
branch_from = "default"       # the trunk; or "current", or a branch name
merge = true                  # on agreement, fast-forward that branch onto the work
```

## A commit per round, on a branch of its own

A repository the agents change is moved onto `dai/<run-id>`, a branch rooted on
whatever it would merge back into — `main`/`master` by default. Each round lands
there as an ordinary commit, and at consensus that base branch is fast-forwarded
onto the result, so afterwards you are standing on your own branch with the work
committed, `git status` clean and `git log` reading as the work having simply
been done.

**Repositories nothing changed in are not touched at all** — no branch, no
commit, no switch. Directories without git are skipped.

Whatever was uncommitted before you started is kept as a `baseline` commit of its
own, so everything after it is the agents' doing.

Neither the switch nor the commits go through `git checkout` or `git commit`,
because both rewrite files and run your hooks — a formatter firing mid-round
would edit the tree under the agents' feet. dai moves HEAD with `symbolic-ref`
and syncs the index with `read-tree`, so **not one file on disk is ever rewritten
by dai and no hook of yours fires**. The one thing it cannot preserve is a
staged-then-edited blob, which lives only in the index: a repository with
anything staged is left alone, and says so.

The directory you run in need not be the repository. It can hold several side by
side, or be no repository at all with every one of them a level down — so the
run, and `dai --snapshots`, tell you *which* repository ended up with what:

```bash
dai --snapshots            # or: make snapshots
```
```
raw-context-codex
  dai/20260815-174936-p77s  3 commits  2 hours ago  dai 20260815-174936-p77s: final
nothing committed in: TODO, arch-claude, presentations, wiki-concept
read one: git -C <repo> log --oneline <branch>
```

```bash
git -C <repo> log --oneline <base>..<branch>  # the rounds
git -C <repo> diff <base> <branch>            # everything they changed
git -C <repo> reset --hard <base>             # undo the lot
git -C <repo> branch -D dai/<run-id>          # forget the run
```

`<base>` is printed in the report, and is used rather than `HEAD` because `HEAD`
stops being the right answer the moment anything moves.

`make clean-runs` deletes transcripts but leaves the branches alone, so you can
still recover the work after it. Rename the prefix with `branch_prefix` under
`[snapshot]`, or switch the whole thing off with `--no-snapshot`.

### Where the branch is rooted, and what it merges into

One setting decides both, because they are the same thing — `branch_from` under
`[snapshot]`, or `--branch-from`:

| | |
|---|---|
| `"default"` | the repository's trunk: `origin/HEAD`, else `main`, `master`, `trunk` |
| `"current"` | the branch you are standing on |
| a name | that branch, e.g. `"develop"` |

If you are *ahead* of the branch named there, dai roots the run where you are
instead: folding your own commits into one `baseline` and carrying them back on
the merge is not something to do quietly.

`merge = true` under `[snapshot]`, or `--no-merge` to turn it off for one run.
**On by default.** Only consensus merges — a deadlock, a run out of budget, or
one you killed with `q` never does, and leaves you on `dai/<run-id>` with the
work committed there and yours to merge by hand.

A repository whose base branch moved during the run is refused, with the reason
printed, and also stays on `dai/<run-id>`. A refusal costs you nothing. Because
merging is on by default, the `baseline` commit holding your own pre-run
work-in-progress lands on your branch too, authored `dai`; the report prints the
undo, `git -C <repo> reset --hard <base>`.

## Notes

- **Codex reports tokens, not cost.** Its spend shows as *unmeasured* rather
  than as zero; add `[pricing.codex]` to the config for an estimate.
- **The claude critic runs read-only (`plan` mode)** and so cannot run your
  tests; the codex critic in `read-only` sandbox can. See `critic_args` in the
  config to change that.
- **Agents read your files** — use `make run-dry` in repositories you don't trust.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 romus.
