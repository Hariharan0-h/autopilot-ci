"""
tests/test_tools.py — Unit tests for AST and SAST tools.
"""
import pytest
from tools.ast_tools import (
    get_functions,
    find_nested_loops,
    find_db_calls_in_loops,
    get_functions_without_docstrings,
)
from tools.sast_tools import check_requirements_cve
from tools.shell_tools import run_command_sync


# ─── ast_tools tests ──────────────────────────────────────────────────────────

SAMPLE_SOURCE = '''
def simple_add(a, b):
    """Add two numbers."""
    return a + b

def no_doc_func(x, y):
    if x > 0:
        return x
    elif y > 0:
        return y
    return 0

def complex_func(items, threshold):
    """Complex function with high cyclomatic complexity."""
    result = []
    for item in items:
        if item > threshold:
            if item % 2 == 0:
                result.append(item * 2)
            elif item % 3 == 0:
                result.append(item * 3)
            else:
                result.append(item)
        elif item == threshold:
            result.append(item)
    return result
'''

NESTED_LOOP_SOURCE = '''
def find_pairs(lst):
    result = []
    for i in range(len(lst)):
        for j in range(len(lst)):
            if i != j and lst[i] + lst[j] == 10:
                result.append((lst[i], lst[j]))
    return result

def single_loop(items):
    return [x * 2 for x in items]
'''

DB_IN_LOOP_SOURCE = '''
def process_users(user_ids, session):
    results = []
    for uid in user_ids:
        user = session.query(uid)
        results.append(user)
    return results
'''


class TestGetFunctions:
    def test_finds_all_functions(self):
        fns = get_functions(SAMPLE_SOURCE)
        names = [f.name for f in fns]
        assert "simple_add" in names
        assert "no_doc_func" in names
        assert "complex_func" in names

    def test_detects_docstring(self):
        fns = get_functions(SAMPLE_SOURCE)
        fn_map = {f.name: f for f in fns}
        assert fn_map["simple_add"].has_docstring is True
        assert fn_map["no_doc_func"].has_docstring is False

    def test_cyclomatic_complexity_base(self):
        fns = get_functions(SAMPLE_SOURCE)
        fn_map = {f.name: f for f in fns}
        # simple_add: no branches → complexity 1
        assert fn_map["simple_add"].cyclomatic_complexity == 1

    def test_cyclomatic_complexity_branches(self):
        fns = get_functions(SAMPLE_SOURCE)
        fn_map = {f.name: f for f in fns}
        # no_doc_func: if + elif + for → at least 3
        assert fn_map["no_doc_func"].cyclomatic_complexity >= 3

    def test_complex_func_higher_complexity(self):
        fns = get_functions(SAMPLE_SOURCE)
        fn_map = {f.name: f for f in fns}
        assert fn_map["complex_func"].cyclomatic_complexity > fn_map["no_doc_func"].cyclomatic_complexity

    def test_start_end_lines(self):
        fns = get_functions(SAMPLE_SOURCE)
        fn_map = {f.name: f for f in fns}
        assert fn_map["simple_add"].start_line < fn_map["no_doc_func"].start_line

    def test_args_captured(self):
        fns = get_functions(SAMPLE_SOURCE)
        fn_map = {f.name: f for f in fns}
        assert fn_map["simple_add"].args == ["a", "b"]

    def test_invalid_syntax_returns_empty(self):
        result = get_functions("def broken(:\n    pass")
        assert result == []


class TestFindNestedLoops:
    def test_detects_nested_for_loop(self):
        hits = find_nested_loops(NESTED_LOOP_SOURCE)
        assert len(hits) >= 1

    def test_nested_loop_has_correct_keys(self):
        hits = find_nested_loops(NESTED_LOOP_SOURCE)
        assert len(hits) > 0
        hit = hits[0]
        assert "line" in hit
        assert "depth" in hit
        assert "snippet" in hit

    def test_nested_loop_depth_at_least_2(self):
        hits = find_nested_loops(NESTED_LOOP_SOURCE)
        assert all(h["depth"] >= 2 for h in hits)

    def test_single_loop_not_flagged(self):
        single = "def f():\n    for x in items:\n        pass"
        hits = find_nested_loops(single)
        assert len(hits) == 0

    def test_invalid_syntax_returns_empty(self):
        assert find_nested_loops("def broken(:\n    pass") == []


class TestFindDbCallsInLoops:
    def test_detects_query_in_loop(self):
        hits = find_db_calls_in_loops(DB_IN_LOOP_SOURCE)
        assert len(hits) >= 1

    def test_hit_has_line_and_snippet(self):
        hits = find_db_calls_in_loops(DB_IN_LOOP_SOURCE)
        assert len(hits) > 0
        assert "line" in hits[0]
        assert "snippet" in hits[0]

    def test_no_db_call_outside_loop(self):
        source = "def f(s):\n    return s.query(1)"
        hits = find_db_calls_in_loops(source)
        assert len(hits) == 0


class TestGetFunctionsWithoutDocstrings:
    def test_returns_public_undocumented(self):
        result = get_functions_without_docstrings(SAMPLE_SOURCE)
        names = [f.name for f in result]
        assert "no_doc_func" in names
        assert "simple_add" not in names  # has docstring

    def test_excludes_private_functions(self):
        src = "def _private():\n    pass\n\ndef public():\n    pass"
        result = get_functions_without_docstrings(src)
        names = [f.name for f in result]
        assert "_private" not in names
        assert "public" in names


# ─── sast_tools tests ─────────────────────────────────────────────────────────

class TestCheckRequirementsCve:
    def test_detects_known_vulnerable(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.18.0\nflask==2.3.0\n")
        hits = check_requirements_cve(str(req))
        assert any("requests==2.18.0" in h for h in hits)

    def test_no_hit_for_safe_version(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests==2.31.0\n")
        hits = check_requirements_cve(str(req))
        assert len(hits) == 0

    def test_returns_empty_for_missing_file(self):
        hits = check_requirements_cve("/nonexistent/requirements.txt")
        assert hits == []

    def test_ignores_comments(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("# requests==2.18.0\nflask==2.3.0\n")
        hits = check_requirements_cve(str(req))
        assert len(hits) == 0


# ─── shell_tools tests ────────────────────────────────────────────────────────

class TestRunCommandSync:
    def test_successful_command(self):
        result = run_command_sync(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_command_not_found(self):
        result = run_command_sync(["nonexistent_command_xyz"])
        assert result.returncode != 0

    def test_timed_out_flag(self):
        result = run_command_sync(["sleep", "5"], timeout_seconds=1)
        assert result.timed_out is True
