# Gold validation

Compared runs: `gold-3`, `gold-4`. Written by `repolace-eval gold`; this tool reads the database and **never edits an instance file**. Proposals below are for the maintainer to apply or ignore.

18 instance(s): 17 accepted, 1 rejected.

## Rejected

| instance | reasons |
| --- | --- |
| `pylint-dev__pylint-4551` | gold-3: outcome is failed, not passed (status completed): 2 expected fail-to-pass test(s) still not passing: tests/unittest_pyreverse_writer.py::test_get_annotation_annassign[a:, tests/unittest_pyreverse_writer.py::test_get_annotation_assignattr[def; gold-4: outcome is failed, not passed (status completed): 2 expected fail-to-pass test(s) still not passing: tests/unittest_pyreverse_writer.py::tes… |

## Accepted

| instance | baseline wall (s) | gold run wall (s) | targeted_p2p |
| --- | --- | --- | --- |
| `mwaskom__seaborn-3069` | 576, 550 | 571, 551 | no |
| `mwaskom__seaborn-3187` | 609, 585 | 623, 587 | no |
| `pallets__flask-5014` | 7, 9 | 9, 7 | no |
| `pylint-dev__pylint-4604` | 581, 562 | 603, 515 | no |
| `pylint-dev__pylint-4970` | 526, 488 | 537, 464 | no |
| `pytest-dev__pytest-5631` | 298, 277 | 280, 260 | no |
| `pytest-dev__pytest-6202` | 354, 305 | 350, 270 | no |
| `pytest-dev__pytest-7236` | 344, 267 | 368, 279 | no |
| `pytest-dev__pytest-7432` | 387, 284 | 393, 289 | no |
| `pytest-dev__pytest-7571` | 404, 297 | 421, 287 | no |
| `pytest-dev__pytest-7982` | 411, 275 | 332, 274 | no |
| `sphinx-doc__sphinx-7462` | 203, 141 | 170, 146 | no |
| `sphinx-doc__sphinx-7590` | 166, 144 | 169, 142 | no |
| `sphinx-doc__sphinx-9230` | 181, 155 | 184, 156 | no |
| `sphinx-doc__sphinx-9320` | 183, 157 | 183, 156 | no |
| `sphinx-doc__sphinx-9367` | 184, 157 | 170, 153 | no |
| `sphinx-doc__sphinx-9602` | 149, 151 | 127, 109 | no |

## targeted_p2p proposals

None.

## Addendum: `pylint-dev__pylint-4551` re-validated in `gold-5`, `gold-6`

Added by hand, not by `repolace-eval gold`. The rejection above is an instrument defect: two of its
curated fail-to-pass ids were cut at a space by SWE-bench (`test_get_annotation_annassign[a:`), so an
exact match could never succeed. `verify.scoring.expand_truncated_ids` now reads such an id as a prefix.
With that fix, `repolace-eval gold --runs gold-5,gold-6` (runs of this instance alone) accepted it:

| instance | baseline wall (s) | gold run wall (s) | targeted_p2p |
| --- | --- | --- | --- |
| `pylint-dev__pylint-4551` | 357, 357 | 355, 351 | no |

All 18 instances are accepted across the two validations.
