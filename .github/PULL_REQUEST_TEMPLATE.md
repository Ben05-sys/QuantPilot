<!--
Thanks for sending this. The checklist is short on purpose — every line is
something that has actually gone wrong here before.
-->

## What this changes

<!-- And why it matters to someone screening real stocks. -->

## How it was verified

<!--
Which suites you ran, and what you looked at in the terminal. If it touches
the UI, say what you clicked.
-->

## Checklist

- [ ] The suites pass: `python tests/test_screen.py` and friends, plus
      `node tests/ui_smoke.js` if `app/web/index.html` changed.
- [ ] `ruff check .` is clean.
- [ ] If this fixes a bug, there is a test that **fails before and passes
      after**. If that test could not be written, the PR says why.
- [ ] No existing assertion was weakened to get a green run.
- [ ] Nothing claims live data over stale data, and no field the terminal
      cannot compute was defaulted into looking like a fact.
- [ ] If a screenable field was added: computed in `derive()`, listed in
      `DERIVED`, given an alias, and classified `LIVE_COLUMNS` or
      `STATIC_SAFE` — see the CONTRIBUTING section on which.
