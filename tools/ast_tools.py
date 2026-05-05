"""ast_tools.py — Python AST analysis for AutoPilot CI."""

import ast
from pathlib import Path
from schemas import FunctionInfo

# ─── Complexity counter ────────────────────────────────────────────────────────

_COMPLEXITY_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.Assert,
)


def _compute_complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute cyclomatic complexity for a function node.

    Counts: base 1 + if/elif/for/while/except/with/assert +
    each BoolOp value beyond the first (each `and`/`or` operand adds 1).

    Args:
        func_node: AST FunctionDef or AsyncFunctionDef node.

    Returns:
        Integer cyclomatic complexity (minimum 1).
    """
    complexity = 1
    for node in ast.walk(func_node):
        if isinstance(node, _COMPLEXITY_NODES):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            # each additional operand in `a and b and c` adds 1
            complexity += len(node.values) - 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # comprehension ifs
            for generator in getattr(node, "generators", []):
                complexity += len(generator.ifs)
    return complexity


# ─── Public functions ──────────────────────────────────────────────────────────

def get_functions(source_code: str, filepath: str = "") -> list[FunctionInfo]:
    """Parse Python source and return FunctionInfo for every function/method.

    Computes cyclomatic_complexity by counting if/elif/for/while/and/or/
    except/with/assert/comprehension ifs plus 1 base.
    Sets has_docstring=True if first statement is a string constant.

    Args:
        source_code: Python source code as a string.
        filepath: Optional file path for FunctionInfo.file field.

    Returns:
        List of FunctionInfo for every function/method found.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    results: list[FunctionInfo] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Extract argument names
        args = [arg.arg for arg in node.args.args]
        # Check for docstring
        has_doc = (
            len(node.body) > 0
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        )
        complexity = _compute_complexity(node)
        end_line = getattr(node, "end_lineno", node.lineno)
        # Extract raw source lines
        lines = source_code.splitlines()
        src_lines = lines[node.lineno - 1 : end_line]
        source = "\n".join(src_lines)

        results.append(FunctionInfo(
            name=node.name,
            file=filepath,
            start_line=node.lineno,
            end_line=end_line,
            args=args,
            has_docstring=has_doc,
            cyclomatic_complexity=complexity,
            source=source,
        ))
    return results


def get_untested_functions(
    source_path: str, test_dir: str
) -> list[FunctionInfo]:
    """Return functions in source_path not referenced by name in any test file.

    A function is 'tested' if its name appears anywhere in a test file found
    recursively under test_dir.

    Args:
        source_path: Path to a Python source file to analyze.
        test_dir: Directory to search recursively for test files.

    Returns:
        List of FunctionInfo for untested functions.
    """
    source_path_obj = Path(source_path)
    if not source_path_obj.exists():
        return []

    source_code = source_path_obj.read_text(encoding="utf-8", errors="replace")
    all_functions = get_functions(source_code, filepath=source_path)
    if not all_functions:
        return []

    # Collect all test file contents
    test_dir_obj = Path(test_dir)
    test_contents = ""
    if test_dir_obj.exists():
        for test_file in test_dir_obj.rglob("*"):
            if not test_file.is_file() or test_file.suffix not in (".py", ".ts", ".js", ".cs", ".java", ".go", ".rb", ".kt"):
                continue
            # only include files that look like test files
            if not any(k in test_file.stem.lower() for k in ("test", "spec")):
                continue
            try:
                test_contents += test_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    # A function is tested if its name appears in test contents
    untested = [
        fn for fn in all_functions
        if fn.name not in test_contents
    ]
    return untested


class _NestedLoopFinder(ast.NodeVisitor):
    """AST visitor that detects loops nested at 2+ levels deep."""

    def __init__(self):
        self.findings: list[dict] = []
        self._depth = 0
        self._lines: list[str] = []

    def _visit_loop(self, node: ast.AST) -> None:
        self._depth += 1
        if self._depth >= 2:
            line_idx = node.lineno - 1
            snippet = (
                self._lines[line_idx].strip()
                if line_idx < len(self._lines) else ""
            )
            self.findings.append({
                "line": node.lineno,
                "depth": self._depth,
                "snippet": snippet,
            })
        self.generic_visit(node)
        self._depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node)


def find_nested_loops(source_code: str) -> list[dict]:
    """Return list of {line, depth, snippet} for loops nested 2+ levels.

    Detects: for-in-for, while-in-for, for-in-while, while-in-while.
    snippet is the inner loop's first line.

    Args:
        source_code: Python source code as a string.

    Returns:
        List of dicts with keys: line (int), depth (int), snippet (str).
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    finder = _NestedLoopFinder()
    finder._lines = source_code.splitlines()
    finder.visit(tree)
    return finder.findings


class _DBCallInLoopFinder(ast.NodeVisitor):
    """AST visitor that detects ORM/DB calls inside loop bodies."""

    _DB_METHODS = frozenset({
        "execute", "query", "filter", "filter_by", "add", "get",
        "fetchone", "fetchall", "fetchmany", "scalar",
    })

    def __init__(self, lines: list[str]):
        self.findings: list[dict] = []
        self._in_loop = False
        self._lines = lines

    def _visit_loop(self, node: ast.AST) -> None:
        old = self._in_loop
        self._in_loop = True
        self.generic_visit(node)
        self._in_loop = old

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_loop and isinstance(node.func, ast.Attribute):
            if node.func.attr in self._DB_METHODS:
                line_idx = node.lineno - 1
                snippet = (
                    self._lines[line_idx].strip()
                    if line_idx < len(self._lines) else ""
                )
                self.findings.append({
                    "line": node.lineno,
                    "snippet": snippet,
                })
        self.generic_visit(node)


def find_db_calls_in_loops(source_code: str) -> list[dict]:
    """Return list of {line, snippet} for ORM/DB calls inside loops (N+1 pattern).

    Detects calls to: execute, query, filter, filter_by, add, get,
    fetchone, fetchall, fetchmany, scalar inside for or while loops.

    Args:
        source_code: Python source code as a string.

    Returns:
        List of dicts with keys: line (int), snippet (str).
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    finder = _DBCallInLoopFinder(lines=source_code.splitlines())
    finder.visit(tree)
    return finder.findings


def get_functions_without_docstrings(
    source_code: str, filepath: str = ""
) -> list[FunctionInfo]:
    """Return only public functions (not starting with _) that lack docstrings.

    Args:
        source_code: Python source code as a string.
        filepath: Optional file path for FunctionInfo metadata.

    Returns:
        List of FunctionInfo for functions missing docstrings.
    """
    return [
        fn for fn in get_functions(source_code, filepath=filepath)
        if not fn.has_docstring and not fn.name.startswith("_")
    ]
