# Gold validation

Compared runs: `gold-1`, `gold-2`. Written by `repolace-eval gold`; this tool reads the database and **never edits an instance file**. Proposals below are for the maintainer to apply or ignore.

18 instance(s): 6 accepted, 12 rejected.

## Rejected

| instance | reasons |
| --- | --- |
| `pallets__flask-5014` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pallets__flask-5014 (exit 1): ...irectory: 'requirements.txt' ------ Dockerfile:9 -------------------- 7 \| WORKDIR /repo 8 \| COPY source/ /repo/ 9 \| &gt;&gt;&gt; RUN pip install -r requirements.txt 10 \| RUN pip install setuptools==70.0.0 click==8.1.3 itsdangerous==2.1.2 Jinja2==3.1.2 Mar… |
| `pylint-dev__pylint-4551` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pylint-dev__pylint-4551 (exit 1): ... requirements.txt: 4.366 ERROR: Could not open requirements file: [Errno 2] No such file or directory: 'requirements.txt' ------ Dockerfile:9 -------------------- 7 \| WORKDIR /repo 8 \| COPY source/ /repo/ 9 \| &gt;&gt;&gt; RUN pip install -r requirement… |
| `pylint-dev__pylint-4604` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pylint-dev__pylint-4604 (exit 1): ... requirements.txt: 4.088 ERROR: Could not open requirements file: [Errno 2] No such file or directory: 'requirements.txt' ------ Dockerfile:9 -------------------- 7 \| WORKDIR /repo 8 \| COPY source/ /repo/ 9 \| &gt;&gt;&gt; RUN pip install -r requirement… |
| `pylint-dev__pylint-4970` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pylint-dev__pylint-4970 (exit 1): ... requirements.txt: 3.405 ERROR: Could not open requirements file: [Errno 2] No such file or directory: 'requirements.txt' ------ Dockerfile:9 -------------------- 7 \| WORKDIR /repo 8 \| COPY source/ /repo/ 9 \| &gt;&gt;&gt; RUN pip install -r requirement… |
| `pytest-dev__pytest-5631` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pytest-dev__pytest-5631 (exit 1): ... not pip. 21.64 hint: See above for details. ------ Dockerfile:10 -------------------- 8 \| COPY source/ /repo/ 9 \| RUN pip install atomicwrites==1.4.1 attrs==23.1.0 more-itertools==10.1.0 packaging==23.1 pluggy==0.13.1 py==1.11.0 wcwidth==0.2.6 10 \| &g… |
| `pytest-dev__pytest-6202` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pytest-dev__pytest-6202 (exit 1): ... not pip. 23.11 hint: See above for details. ------ Dockerfile:10 -------------------- 8 \| COPY source/ /repo/ 9 \| RUN pip install atomicwrites==1.4.1 attrs==23.1.0 more-itertools==10.1.0 packaging==23.1 pluggy==0.13.1 py==1.11.0 wcwidth==0.2.6 10 \| &g… |
| `pytest-dev__pytest-7236` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pytest-dev__pytest-7236 (exit 1): ...e with the package mentioned above, not pip. 23.48 hint: See above for details. ------ Dockerfile:10 -------------------- 8 \| COPY source/ /repo/ 9 \| RUN pip install py==1.11.0 packaging==23.1 attrs==23.1.0 more-itertools==10.1.0 pluggy==0.13.1 10 \| &g… |
| `pytest-dev__pytest-7432` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pytest-dev__pytest-7432 (exit 1): ...e with the package mentioned above, not pip. 21.76 hint: See above for details. ------ Dockerfile:10 -------------------- 8 \| COPY source/ /repo/ 9 \| RUN pip install py==1.11.0 packaging==23.1 attrs==23.1.0 more-itertools==10.1.0 pluggy==0.13.1 10 \| &g… |
| `pytest-dev__pytest-7571` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pytest-dev__pytest-7571 (exit 1): ...bove, not pip. 22.72 hint: See above for details. ------ Dockerfile:10 -------------------- 8 \| COPY source/ /repo/ 9 \| RUN pip install attrs==23.1.0 iniconfig==2.0.0 more-itertools==10.1.0 packaging==23.1 pluggy==0.13.1 py==1.11.0 toml==0.10.2 10 \| &g… |
| `pytest-dev__pytest-7982` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: environment build failed for pytest-dev__pytest-7982 (exit 1): ...the package mentioned above, not pip. 23.03 hint: See above for details. ------ Dockerfile:10 -------------------- 8 \| COPY source/ /repo/ 9 \| RUN pip install attrs==23.1.0 iniconfig==2.0.0 packaging==23.1 pluggy==0.13.1 py==1.11.0 toml==0.10.2 10 \| &g… |
| `sphinx-doc__sphinx-7462` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: verify: no test report was written (exit 1); a usage error or a startup failure runs before any plugin hook; gold-2: outcome is none, not passed (status completed): baseline unscoreable: verify: no test report was written (exit 1); a usage error or a startup failure runs before any plugin hook; gold-1: baseline is unsco… |
| `sphinx-doc__sphinx-7590` | gold-1: outcome is none, not passed (status completed): baseline unscoreable: verify: no test report was written (exit 1); a usage error or a startup failure runs before any plugin hook; gold-2: outcome is none, not passed (status completed): baseline unscoreable: verify: no test report was written (exit 1); a usage error or a startup failure runs before any plugin hook; gold-1: baseline is unsco… |

## Accepted

| instance | baseline wall (s) | gold run wall (s) | targeted_p2p |
| --- | --- | --- | --- |
| `mwaskom__seaborn-3069` | 1033, 476 | 1068, 474 | proposed |
| `mwaskom__seaborn-3187` | 1085, 508 | 1162, 519 | proposed |
| `sphinx-doc__sphinx-9230` | 304, 151 | 149, 148 | no |
| `sphinx-doc__sphinx-9320` | 222, 153 | 190, 167 | no |
| `sphinx-doc__sphinx-9367` | 150, 142 | 275, 127 | no |
| `sphinx-doc__sphinx-9602` | 106, 131 | 106, 120 | no |

## targeted_p2p proposals

### `mwaskom__seaborn-3069`

- longest suite run 1068s is at least 50% of the 1800s timeout
- proposed `spec.test_targets`: `tests/_core/test_plot.py`
- would still cover 94 of 94 pass-to-pass ids
- applying it means setting `targeted_p2p` to true as well; the two must agree

### `mwaskom__seaborn-3187`

- longest suite run 1162s is at least 50% of the 1800s timeout
- proposed `spec.test_targets`: `tests/_core/test_plot.py`, `tests/test_relational.py`
- would still cover 248 of 248 pass-to-pass ids
- applying it means setting `targeted_p2p` to true as well; the two must agree
