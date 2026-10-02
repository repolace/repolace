"""Results that are sized by character budget, so their header and trailer tell the truth.

The failure these hold against: a tool sized its result by item count (400 lines), the
box then cut it at the output cap (8000 characters) a quarter of the way through, and
the header and the "continue from line 401" hint described the uncut result. A model that
trusts them skips every line in between and never learns it. Each test here follows the
tool's own continuation hint to the end and checks nothing was skipped.
"""

import re

import pytest

from repolace_agents.tools import ToolLimits

from tools_support import hit, make_harness, git

pytestmark = pytest.mark.anyio

CAP = ToolLimits().max_output_chars


def ordinary_lines(count: int) -> list[str]:
    """Lines like real source: ~58 characters, so 400 of them are ~23k characters."""
    return [f"    value_{number:04} = compute(argument_one, argument_two)  # {number}" for number in range(1, count + 1)]


def shown_line_numbers(content: str) -> list[int]:
    return [int(match.group(1)) for match in re.finditer(r"^\s*(\d+)\t", content, re.MULTILINE)]


class TestReadFileHeaderTellsTheTruth:
    async def test_a_default_call_on_a_long_file_reports_exactly_what_it_shows(self, tmp_path):
        h = make_harness(tmp_path)
        (h.checkout / "src/pkg/long.py").write_text("\n".join(ordinary_lines(1000)) + "\n")

        out = await h.call("read_file", path="src/pkg/long.py")

        header, *_ = out.content.splitlines()
        claimed_last = int(re.search(r"lines 1-(\d+) of 1000", header).group(1))
        numbers = shown_line_numbers(out.content)
        assert numbers == list(range(1, claimed_last + 1))  # every claimed line is there, none missing
        assert claimed_last < 400  # the output cap, not the line cap, is what binds on ordinary lines
        assert len(out.content) <= CAP and "[truncated" not in out.content
        assert f"start_line={claimed_last + 1} to continue" in out.content

    async def test_the_last_line_shown_is_a_whole_line(self, tmp_path):
        h = make_harness(tmp_path)
        lines = ordinary_lines(1000)
        (h.checkout / "src/pkg/long.py").write_text("\n".join(lines) + "\n")

        out = await h.call("read_file", path="src/pkg/long.py")

        last_shown = [line for line in out.content.splitlines() if re.match(r"^\s*\d+\t", line)][-1]
        number = int(last_shown.split("\t")[0])
        assert last_shown == f"{number:>6}\t{lines[number - 1]}"

    async def test_following_the_hints_reads_every_line_exactly_once(self, tmp_path):
        h = make_harness(tmp_path)
        lines = ordinary_lines(1000)
        (h.checkout / "src/pkg/long.py").write_text("\n".join(lines) + "\n")

        seen: list[int] = []
        start = 1
        while start <= 1000:
            out = await h.call("read_file", path="src/pkg/long.py", start_line=start)
            numbers = shown_line_numbers(out.content)
            assert numbers and numbers[0] == start, out.content[:200]
            seen.extend(numbers)
            hint = re.search(r"start_line=(\d+) to continue", out.content)
            start = int(hint.group(1)) if hint else numbers[-1] + 1

        assert seen == list(range(1, 1001))

    async def test_a_short_file_has_no_continuation_hint(self, tmp_path):
        h = make_harness(tmp_path)
        (h.checkout / "src/pkg/short.py").write_text("\n".join(ordinary_lines(20)) + "\n")

        out = await h.call("read_file", path="src/pkg/short.py")

        assert "lines 1-20 of 20" in out.content and "to continue" not in out.content

    async def test_an_explicit_range_that_fits_has_no_hint_and_one_that_does_not_has_one(self, tmp_path):
        h = make_harness(tmp_path)
        (h.checkout / "src/pkg/long.py").write_text("\n".join(ordinary_lines(1000)) + "\n")

        fits = await h.call("read_file", path="src/pkg/long.py", start_line=10, end_line=20)
        too_big = await h.call("read_file", path="src/pkg/long.py", start_line=10, end_line=400)

        assert "to continue" not in fits.content
        claimed_last = int(re.search(r"lines 10-(\d+) of 1000", too_big.content).group(1))
        assert claimed_last < 400 and f"start_line={claimed_last + 1} to continue" in too_big.content

    async def test_one_line_longer_than_the_budget_is_cut_and_says_so(self, tmp_path):
        h = make_harness(tmp_path)
        (h.checkout / "src/pkg/min.js").write_text("x" * 50_000 + "\nsecond line\n")

        out = await h.call("read_file", path="src/pkg/min.js")

        assert "lines 1-1 of 2" in out.content
        assert "[line cut: 50000 characters long]" in out.content
        assert "start_line=2 to continue" in out.content
        assert len(out.content) <= CAP and "[truncated" not in out.content

    async def test_a_small_output_cap_is_still_honest(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_output_chars=600))
        (h.checkout / "src/pkg/long.py").write_text("\n".join(ordinary_lines(100)) + "\n")

        out = await h.call("read_file", path="src/pkg/long.py")

        claimed_last = int(re.search(r"lines 1-(\d+) of 100", out.content).group(1))
        assert shown_line_numbers(out.content) == list(range(1, claimed_last + 1))
        assert len(out.content) <= 600 and "[truncated" not in out.content


class TestListDirKeepsItsTrailer:
    async def test_entries_with_long_names_are_cut_whole_and_the_trailer_survives(self, tmp_path):
        h = make_harness(tmp_path)
        for number in range(150):
            (h.checkout / f"{number:03}_{'n' * 120}.txt").write_text("x")

        out = await h.call("list_dir")

        entries = [line for line in out.content.splitlines()[1:] if not line.startswith("[")]
        assert len(out.content) <= CAP and "[truncated" not in out.content
        assert out.content.splitlines()[-1].startswith("[") and "cut at the output limit" in out.content
        assert all(entry.endswith(".txt") or entry.endswith("/") for entry in entries)  # no half-name
        assert f"[{len([n for n in h.checkout.iterdir() if not n.name.startswith('.git')]) - len(entries)} more entries" in out.content


class TestSearchCodeKeepsWholeHits:
    async def test_hits_that_do_not_fit_are_dropped_whole_with_a_count(self, tmp_path):
        snippet = "\n".join(f"line {number}: " + "y" * 190 for number in range(40))
        hits = [hit(f"src/mod_{number}.py", 1, 40, f"fn_{number}", snippet) for number in range(8)]
        h = make_harness(tmp_path, hits=hits)

        out = await h.call("search_code", query="anything")

        shown = out.content.count("(function)")
        assert 1 <= shown < 8
        assert len(out.content) <= CAP and "[truncated" not in out.content
        assert f"[{8 - shown} more hit(s) cut at the output limit" in out.content
        assert out.content.count("    line ") == shown * 30  # every shown hit has its whole snippet

    async def test_a_single_oversized_hit_is_shortened_not_dropped(self, tmp_path):
        snippet = "\n".join("z" * 200 for _ in range(30))
        h = make_harness(tmp_path, hits=[hit(snippet=snippet)], limits=ToolLimits(max_output_chars=1500))

        out = await h.call("search_code", query="anything")

        assert out.content.startswith("src/pkg/core.py:1-2 add (function)") and len(out.content) <= 1500


class TestGrepKeepsItsNotices:
    async def test_many_long_matches_stop_at_the_budget_and_the_count_is_exact(self, tmp_path):
        files = {"src/pkg/wide.txt": "\n".join(f"needle {number} " + "w" * 280 for number in range(200)) + "\n"}
        h = make_harness(tmp_path, files=files)
        git(h.checkout, "add", "-A")

        out = await h.call("grep", pattern="needle", fixed_string=True, max_results=200)

        lines = out.content.splitlines()
        shown = [line for line in lines if line.startswith("src/pkg/wide.txt:")]
        notice = lines[-1]
        assert len(out.content) <= CAP and "[truncated" not in out.content
        assert notice == f"[showing the first {len(shown)} of 200 matches; narrow with path or glob, or use a more specific pattern]"
        assert 0 < len(shown) < 200
