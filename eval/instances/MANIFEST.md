# Instance manifest

Written by `repolace-eval select`. The filter is auditable from this file alone: every candidate, and every instance that was rejected with the first reason it failed.

- dataset: `princeton-nlp/SWE-bench_Verified` (500 rows)
- seed: 0; target: 25 candidates; at most 6 per repository. The seed is a degree of freedom in the result: fix and record it before the first agent run.
- selected: 18; rejected: 37; not evaluated (target or per-repo cap reached): 51

## Selected

| instance | repo | version | base image | FAIL_TO_PASS | PASS_TO_PASS | test files | gold files | system packages |
|---|---|---|---|---|---|---|---|---|
| mwaskom__seaborn-3069 | mwaskom/seaborn | 0.12 | python:3.9-slim | 2 | 94 | 1 | 1 | no |
| mwaskom__seaborn-3187 | mwaskom/seaborn | 0.12 | python:3.9-slim | 2 | 248 | 2 | 2 | no |
| pallets__flask-5014 | pallets/flask | 2.3 | python:3.11-slim | 1 | 59 | 1 | 1 | no |
| pylint-dev__pylint-4551 | pylint-dev/pylint | 2.9 | python:3.9-slim | 10 | 0 | 1 | 4 | no |
| pylint-dev__pylint-4604 | pylint-dev/pylint | 2.9 | python:3.9-slim | 21 | 0 | 1 | 2 | no |
| pylint-dev__pylint-4970 | pylint-dev/pylint | 2.10 | python:3.9-slim | 1 | 17 | 1 | 1 | no |
| pytest-dev__pytest-5631 | pytest-dev/pytest | 5.0 | python:3.9-slim | 1 | 15 | 1 | 1 | no |
| pytest-dev__pytest-6202 | pytest-dev/pytest | 5.2 | python:3.9-slim | 1 | 72 | 1 | 1 | no |
| pytest-dev__pytest-7236 | pytest-dev/pytest | 5.4 | python:3.9-slim | 1 | 51 | 1 | 1 | no |
| pytest-dev__pytest-7432 | pytest-dev/pytest | 5.4 | python:3.9-slim | 1 | 77 | 1 | 1 | no |
| pytest-dev__pytest-7571 | pytest-dev/pytest | 6.0 | python:3.9-slim | 1 | 14 | 1 | 1 | no |
| pytest-dev__pytest-7982 | pytest-dev/pytest | 6.2 | python:3.9-slim | 1 | 78 | 1 | 1 | no |
| sphinx-doc__sphinx-7462 | sphinx-doc/sphinx | 3.1 | python:3.9-slim | 2 | 49 | 2 | 2 | no |
| sphinx-doc__sphinx-7590 | sphinx-doc/sphinx | 3.1 | python:3.9-slim | 1 | 24 | 1 | 3 | no |
| sphinx-doc__sphinx-9230 | sphinx-doc/sphinx | 4.1 | python:3.9-slim | 1 | 44 | 1 | 1 | no |
| sphinx-doc__sphinx-9320 | sphinx-doc/sphinx | 4.1 | python:3.9-slim | 1 | 9 | 1 | 1 | no |
| sphinx-doc__sphinx-9367 | sphinx-doc/sphinx | 4.1 | python:3.9-slim | 1 | 25 | 1 | 1 | no |
| sphinx-doc__sphinx-9602 | sphinx-doc/sphinx | 4.2 | python:3.9-slim | 1 | 45 | 1 | 1 | no |

## Rejected, by reason

- `environment`: 30
- `gold-patch`: 1
- `symlink-or-submodule`: 6

## Every rejection

| instance | reason |
|---|---|
| psf__requests-1142 | environment: psf/requests@1.1: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-1724 | environment: psf/requests@2.0: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-1766 | environment: psf/requests@2.0: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-1921 | environment: psf/requests@2.3: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-2317 | environment: psf/requests@2.4: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-2931 | environment: psf/requests@2.9: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-5414 | environment: psf/requests@2.26: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| psf__requests-6028 | environment: psf/requests@2.27: packages='pytest' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-2905 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-3095 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-3151 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-3305 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-3677 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-3993 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4075 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4094 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4356 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4629 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4687 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4695 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-4966 | environment: pydata/xarray@0.12: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-6461 | environment: pydata/xarray@2022.03: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-6599 | environment: pydata/xarray@2022.03: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-6721 | environment: pydata/xarray@2022.06: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-6744 | environment: pydata/xarray@2022.06: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-6938 | environment: pydata/xarray@2022.06: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-6992 | environment: pydata/xarray@2022.06: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-7229 | environment: pydata/xarray@2022.09: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-7233 | environment: pydata/xarray@2022.09: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pydata__xarray-7393 | environment: pydata/xarray@2022.09: packages='environment.yml' needs a conda environment, which a pip-based image cannot reproduce; the instance is excluded rather than approximated |
| pylint-dev__pylint-4661 | gold-patch: touches a protected (test, config or .github) path: setup.cfg |
| pylint-dev__pylint-6386 | symlink-or-submodule: 2 symlink/gitlink entries at the base commit: tests/functional/s/symlink/_binding/__init__.py, tests/functional/s/symlink/_binding/symlink_module.py |
| pylint-dev__pylint-6528 | symlink-or-submodule: 2 symlink/gitlink entries at the base commit: tests/functional/s/symlink/_binding/__init__.py, tests/functional/s/symlink/_binding/symlink_module.py |
| pylint-dev__pylint-6903 | symlink-or-submodule: 2 symlink/gitlink entries at the base commit: tests/functional/s/symlink/_binding/__init__.py, tests/functional/s/symlink/_binding/symlink_module.py |
| pylint-dev__pylint-7080 | symlink-or-submodule: 2 symlink/gitlink entries at the base commit: tests/functional/s/symlink/_binding/__init__.py, tests/functional/s/symlink/_binding/symlink_module.py |
| pylint-dev__pylint-7277 | symlink-or-submodule: 2 symlink/gitlink entries at the base commit: tests/functional/s/symlink/_binding/__init__.py, tests/functional/s/symlink/_binding/symlink_module.py |
| pylint-dev__pylint-8898 | symlink-or-submodule: 2 symlink/gitlink entries at the base commit: tests/functional/s/symlink/_binding/__init__.py, tests/functional/s/symlink/_binding/symlink_module.py |

## Not evaluated

These passed every cheap filter and were not run through the git filters because the target or their repository's cap was reached: pytest-dev__pytest-10051, pytest-dev__pytest-10081, pytest-dev__pytest-10356, pytest-dev__pytest-5262, pytest-dev__pytest-5787, pytest-dev__pytest-5809, pytest-dev__pytest-5840, pytest-dev__pytest-6197, pytest-dev__pytest-7205, pytest-dev__pytest-7324, pytest-dev__pytest-7490, pytest-dev__pytest-7521, pytest-dev__pytest-8399, sphinx-doc__sphinx-10323, sphinx-doc__sphinx-10435, sphinx-doc__sphinx-10449, sphinx-doc__sphinx-10466, sphinx-doc__sphinx-10614, sphinx-doc__sphinx-10673, sphinx-doc__sphinx-11445, sphinx-doc__sphinx-11510, sphinx-doc__sphinx-7440, sphinx-doc__sphinx-7454, sphinx-doc__sphinx-7748, sphinx-doc__sphinx-7757, sphinx-doc__sphinx-7889, sphinx-doc__sphinx-7910, sphinx-doc__sphinx-7985, sphinx-doc__sphinx-8035, sphinx-doc__sphinx-8056, sphinx-doc__sphinx-8120, sphinx-doc__sphinx-8265, sphinx-doc__sphinx-8269, sphinx-doc__sphinx-8459, sphinx-doc__sphinx-8475, sphinx-doc__sphinx-8548, sphinx-doc__sphinx-8551, sphinx-doc__sphinx-8593, sphinx-doc__sphinx-8595, sphinx-doc__sphinx-8621, sphinx-doc__sphinx-8638, sphinx-doc__sphinx-8721, sphinx-doc__sphinx-9229, sphinx-doc__sphinx-9258, sphinx-doc__sphinx-9281, sphinx-doc__sphinx-9461, sphinx-doc__sphinx-9591, sphinx-doc__sphinx-9658, sphinx-doc__sphinx-9673, sphinx-doc__sphinx-9698, sphinx-doc__sphinx-9711

## Excluded by repository

Rows outside the allowlist are counted, not listed (the exclusion is by design, see `ALLOWED_REPOS`): astropy/astropy (22), django/django (231), matplotlib/matplotlib (34), scikit-learn/scikit-learn (32), sympy/sympy (75).
