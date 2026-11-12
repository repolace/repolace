# The demo run

A real example of repolace working end to end, small enough to read in a few minutes.

## What you can look at

- **A public pull request** that the agent opened: [demo-config-parser pull request 2](https://github.com/repolace/demo-config-parser/pull/2), for [issue 1](https://github.com/repolace/demo-config-parser/issues/1) in the same repository.
- **A replay page**, [repolace.rounakkumarsingh.com/demo/](https://repolace.rounakkumarsingh.com/demo/): the stages with their timings, the tests before and after, the model calls with their cost, the diff, and a short video made of screenshots of the real app for that run (taken after it finished; not a live screen recording). It is read-only and is built from the run's recorded data, so nothing on it runs anything.

## How it was made

1. A small Python project, `demo-config-parser` (a `KEY = VALUE` settings parser), was written with one failing test that reproduces a bug: a `#` inside a quoted value was treated as the start of a comment.
2. The bug was filed as an issue that shows the symptom and the expected result, without hinting at the fix.
3. The repository was registered with the local app through the GitHub App installation, and a worker was started.
4. One task was started from the task page. This first run is the one the replay and the pull request above come from: nothing was re-run or chosen from several tries.
5. The run's database rows were exported with `docs/tools/export_demo.py` into the page's data file. The export keeps counts, the diff and timings, and leaves out host paths, container names, error text, logins and test names.
6. The same issue was run a second time, for a screen recording of the app that the page showed at first and that the screenshot video has since replaced. That second run passed too, but cost $0.83 over 25 model calls against $0.29 over 15, so the model's behaviour varies between runs. Its pull request was closed with a comment afterwards so the issue has one open pull request, and there was no third run.

## What it shows, and what it does not

It shows the whole path working once: a baseline test run (7 passed, 1 failed), an edit, a scored run (8 passed), a pull request with the agent's account of the change, and a recorded cost of about thirty cents.

It is **one run on a toy repository built for this purpose**, so it says nothing about how often the agent succeeds. The measured success rate is in the [benchmark](benchmark.md): 29 of 54 on 18 real issues, with its caveats.

## Problems found in this very run

Checking the runs against their own output found three things; the first two are on the replay page and the third is recorded in the repository's open questions:

- The pull request text says "the repository had no failing test for this issue", which is wrong here (the repository did have one, and it turned green). It was a fixed template line that ignored a baseline failure turning green. It has been fixed since: a pull request opened now says that a test which failed before the change passes after it, and that this is not proof the issue is fixed. The demo pull requests keep the old text.
- The agent's fix handles the issue's example and the tests, but it introduces a small regression: an apostrophe inside an unquoted value (`msg = it's fine # comment`) is now read as an opening quote that never closes, so the trailing comment stays in the value. No test covers that, which is the reason the pull request says no person has reviewed it.
- The second run did not start by itself. The app's attempt to hand the task to the queue failed quietly with a broken pipe, because its queue client had kept a connection from before the broker was restarted, and the client did not reconnect. The row stayed queued until it was published by hand; left alone, the sweeper would have republished it after about five minutes. Fixed since: the client now replaces a dropped connection and tries the publish once more.
