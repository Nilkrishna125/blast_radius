import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path("/data")
BASE = Path("/app/base")
MAP = Path("/app/map")
MAX_WORKERS = 8
SYMBOL_CAP = 600

HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
COND_RE = re.compile(r"(\bif\b|\belif\b|\bwhile\b|\band\b|\bor\b|\bnot\b|==|!=|<=|>=|\bis\b|\bin\b)")
DEF_RE = re.compile(r"\s*(async\s+def|def|class)\b")
IMPORT_RE = re.compile(r"\s*(import|from)\b")
CALL_RE = re.compile(r"\(")
ASSIGN_RE = re.compile(r"\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=")
POS_RE = re.compile(r"\b(lineno|col_offset|end_lineno|end_col_offset|token_line)\b")
POSITION_FILES = {
    "sqlglot/tokens.py",
    "sqlglot/tokenizer_core.py",
    "sqlglot/parser.py",
    "sqlglot/errors.py",
}


def load():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_number = {t["number"]: t for t in tests}
    with gzip.open(MAP / "inv.json.gz", "rt") as handle:
        inv = json.load(handle)
    fixtures = json.loads((MAP / "fixtures.json").read_text())
    by_file = {}
    for test in tests:
        by_file.setdefault(test["file"], []).append(test["number"])
    return tests, by_number, set(by_number), {t["file"] for t in tests}, inv, fixtures, by_file


TESTS, TESTS_BY_NUMBER, ALL, TEST_FILES, INV, FIXTURES, TESTS_BY_FILE = load()


def parse_patch(path):
    files = {}
    cur = None
    hunk = None
    old_line = None
    old_path = None
    prev_plus = False
    for raw in path.read_text(errors="replace").splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" ")
            if len(parts) >= 4:
                left = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                right = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                cur = right
                files.setdefault(cur, {"new": False, "deleted": False, "rename": False, "binary": False, "hunks": []})
                old_path = left
            else:
                cur = None
                old_path = None
            hunk = None
            prev_plus = False
        elif raw.startswith("--- "):
            value = raw[4:].strip()
            old_path = value[2:] if value.startswith("a/") else value
        elif raw.startswith("+++ "):
            value = raw[4:].strip()
            new_path = value[2:] if value.startswith("b/") else value
            if new_path == "/dev/null":
                cur = old_path
                meta = {"new": False, "deleted": True, "rename": False, "binary": False, "hunks": []}
            elif old_path == "/dev/null":
                cur = new_path
                meta = {"new": True, "deleted": False, "rename": False, "binary": False, "hunks": []}
            else:
                cur = new_path
                meta = {"new": False, "deleted": False, "rename": False, "binary": False, "hunks": []}
            files[cur] = meta
            hunk = None
            prev_plus = False
        elif raw.startswith("@@") and cur:
            match = HUNK_RE.match(raw)
            if match:
                hunk = {
                    "old_start": int(match.group(1)),
                    "old_count": int(match.group(2)) if match.group(2) is not None else 1,
                    "new_start": int(match.group(3)),
                    "new_count": int(match.group(4)) if match.group(4) is not None else 1,
                    "changed_old": [],
                    "insert_at": [],
                    "added": [],
                    "seq": [],
                }
                files[cur]["hunks"].append(hunk)
                old_line = hunk["old_start"]
                prev_plus = False
        elif raw.startswith("rename ") or raw.startswith("copy "):
            if cur:
                files[cur]["rename"] = True
        elif raw.startswith("Binary files "):
            if cur:
                files[cur]["binary"] = True
        elif hunk is not None:
            if raw.startswith("\\"):
                continue
            tag = raw[:1]
            text = raw[1:]
            if tag == "+":
                hunk["added"].append(text)
                hunk["seq"].append(("+", text))
                if not prev_plus:
                    hunk["insert_at"].append(old_line)
                prev_plus = True
            elif tag == "-":
                hunk["changed_old"].append(old_line)
                hunk["seq"].append(("-", text))
                old_line += 1
                prev_plus = False
            elif tag == " ":
                hunk["seq"].append((" ", text))
                old_line += 1
                prev_plus = False
    return files


def patched_text(base_text, hunks):
    lines = base_text.splitlines()
    for hunk in sorted(hunks, key=lambda item: item["old_start"], reverse=True):
        index = hunk["old_start"] if hunk["old_count"] == 0 else hunk["old_start"] - 1
        segment = [text for tag, text in hunk["seq"] if tag != "-"]
        lines[index:index + hunk["old_count"]] = segment
    return "\n".join(lines)


def syntax_ok(path, hunks):
    source = BASE / path
    if not source.exists():
        return False
    try:
        ast.parse(patched_text(source.read_text(errors="replace"), hunks))
        return True
    except Exception:
        return False


_BLOCKS = {}


def blocks(path):
    if path in _BLOCKS:
        return _BLOCKS[path]
    source = BASE / path
    if not source.exists():
        _BLOCKS[path] = None
        return None
    try:
        tree = ast.parse(source.read_text(errors="replace"))
    except Exception:
        _BLOCKS[path] = None
        return None
    funcs = []
    classes = []
    class_assigns = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            end = getattr(node, "end_lineno", node.lineno)
            if isinstance(node, ast.Lambda):
                body = node.body.lineno
            else:
                body = node.body[0].lineno if node.body else node.lineno
            funcs.append((start, end, body, getattr(node, "name", "<lambda>")))
        elif isinstance(node, ast.ClassDef):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = getattr(node, "end_lineno", node.lineno)
            classes.append((start, end, node.name))
            for statement in node.body:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            class_assigns.append(
                                (statement.lineno, getattr(statement, "end_lineno", statement.lineno), target.id, start, end)
                            )
    _BLOCKS[path] = (funcs, classes, class_assigns)
    return _BLOCKS[path]


def line_tests(path, line):
    return set(INV.get(path, {}).get(str(line), []))


def range_tests(path, low, high):
    found = set()
    for key, numbers in INV.get(path, {}).items():
        if low <= int(key) <= high:
            found.update(numbers)
    return found


_MODULE = {}


def module_tests(path):
    if path in _MODULE:
        return _MODULE[path]
    found = set()
    for numbers in INV.get(path, {}).values():
        found.update(numbers)
    _MODULE[path] = found
    return found


def enclosing_func(path, line):
    info = blocks(path)
    if not info:
        return None
    candidates = [item for item in info[0] if item[0] <= line <= item[1]]
    return min(candidates, key=lambda item: item[1] - item[0]) if candidates else None


def enclosing_class(path, line):
    info = blocks(path)
    if not info:
        return None
    candidates = [item for item in info[1] if item[0] <= line <= item[1]]
    return max(candidates, key=lambda item: item[1] - item[0]) if candidates else None


def enclosing_class_assign(path, line):
    info = blocks(path)
    if not info:
        return None
    candidates = [item for item in info[2] if item[0] <= line <= item[1]]
    return min(candidates, key=lambda item: item[1] - item[0]) if candidates else None


def is_cosmetic(lines):
    return all((not line.strip()) or line.strip().startswith("#") for line in lines)


_SYMBOLS = {}


def symbol_tests(symbol):
    if symbol in _SYMBOLS:
        return _SYMBOLS[symbol]
    found = set()
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    for root, _, names in os.walk(BASE / "sqlglot"):
        for name in names:
            if not name.endswith(".py"):
                continue
            source = Path(root) / name
            path = str(source.relative_to(BASE))
            try:
                lines = source.read_text(errors="replace").splitlines()
            except Exception:
                continue
            for index, text in enumerate(lines, 1):
                if pattern.search(text):
                    func = enclosing_func(path, index)
                    if func:
                        found.update(range_tests(path, func[0], func[1]))
                    else:
                        found.update(line_tests(path, index))
    _SYMBOLS[symbol] = found
    return found


_DIALECTS = {}


def dialect_tests(path):
    stem = os.path.splitext(path.split("/")[-1])[0]
    if stem in _DIALECTS:
        return _DIALECTS[stem]
    found = set()
    for candidate in (
        f"sqlglot/dialects/{stem}.py",
        f"sqlglot/parsers/{stem}.py",
        f"sqlglot/generators/{stem}.py",
        f"sqlglot/typing/{stem}.py",
    ):
        found.update(module_tests(candidate))
    _DIALECTS[stem] = found
    return found


def is_dialect_file(path):
    return any(part in path for part in ("/dialects/", "/parsers/", "/generators/", "/typing/"))


_POSITIVE = {}


def pos_sensitive(path):
    if path in _POSITIVE:
        return _POSITIVE[path]
    if path in POSITION_FILES:
        _POSITIVE[path] = True
        return True
    source = BASE / path
    try:
        value = bool(POS_RE.search(source.read_text(errors="replace")))
    except Exception:
        value = True
    _POSITIVE[path] = value
    return value


def is_helper(path):
    return not os.path.basename(path).startswith("test_")


def assign_symbol(lines):
    for line in lines:
        match = ASSIGN_RE.match(line)
        if match:
            return match.group(1)
    return None


def class_assign_selection(path, symbol, class_start, class_end):
    if is_dialect_file(path):
        refs = symbol_tests(symbol)
        if len(refs) <= SYMBOL_CAP:
            return range_tests(path, class_start, class_end) | refs
        return dialect_tests(path)
    selected = range_tests(path, class_start, class_end)
    refs = symbol_tests(symbol)
    if len(refs) <= SYMBOL_CAP:
        return selected | refs
    cls = enclosing_class(path, class_start)
    if cls:
        class_refs = symbol_tests(cls[2])
        if class_refs:
            return selected | class_refs
    return selected


def select(files):
    selected = set()
    for path, meta in files.items():
        if meta["rename"] or meta["binary"]:
            selected.update(ALL)
            continue
        if meta["deleted"]:
            if path.startswith("tests/fixtures/"):
                stem = os.path.splitext(os.path.basename(path))[0]
                selected.update(FIXTURES.get(stem, ALL))
            elif path.startswith("tests/") and path.endswith(".py") and path in TEST_FILES:
                selected.update(TESTS_BY_FILE.get(path, []))
            else:
                selected.update(ALL)
            continue
        if meta["new"]:
            if path.startswith("tests/fixtures/"):
                continue
            if path.startswith("tests/") and path.endswith(".py"):
                if is_helper(path):
                    selected.update(ALL)
                continue
            if path.endswith(".py") and path.startswith("sqlglot/"):
                continue
            if path.endswith(".py"):
                selected.update(ALL)
            continue
        if path.startswith("tests/") and path.endswith(".py"):
            if path in TEST_FILES:
                selected.update(TESTS_BY_FILE.get(path, []))
            elif is_helper(path):
                selected.update(ALL)
            continue
        if path.startswith("tests/"):
            stem = os.path.splitext(os.path.basename(path))[0]
            selected.update(FIXTURES.get(stem, ALL))
            continue
        if not path.endswith(".py"):
            if path.endswith("py.typed"):
                continue
            found = module_tests(path)
            selected.update(found if found else ALL)
            continue
        if path == "sqlglot/__init__.py":
            selected.update(ALL)
            continue
        if not syntax_ok(path, meta["hunks"]):
            selected.update(ALL)
            continue
        info = blocks(path)
        if info is None:
            selected.update(ALL)
            continue
        source = (BASE / path).read_text(errors="replace").splitlines()

        def text(line):
            return source[line - 1] if 0 <= line - 1 < len(source) else ""

        for hunk in meta["hunks"]:
            added = hunk["added"]
            if hunk["changed_old"]:
                control = any(COND_RE.search(line) for line in added)
                for line in hunk["changed_old"]:
                    current = text(line)
                    if DEF_RE.match(current) or IMPORT_RE.match(current):
                        selected.update(ALL)
                        continue
                    func = enclosing_func(path, line)
                    if func:
                        if control or COND_RE.search(current) or line <= func[2]:
                            selected.update(range_tests(path, func[0], func[1]))
                        else:
                            selected.update(line_tests(path, line))
                        continue
                    assign = enclosing_class_assign(path, line)
                    if assign:
                        _, _, symbol, class_start, class_end = assign
                        selected.update(class_assign_selection(path, symbol, class_start, class_end))
                        continue
                    cls = enclosing_class(path, line)
                    if cls:
                        selected.update(range_tests(path, cls[0], cls[1]))
                        continue
                    if IMPORT_RE.match(current) or DEF_RE.match(current):
                        selected.update(ALL)
                        continue
                    symbol = None
                    match = ASSIGN_RE.match(current)
                    if match:
                        symbol = match.group(1)
                    if symbol is None:
                        symbol = assign_symbol(added)
                    if symbol:
                        selected.update(module_tests(path))
                        refs = symbol_tests(symbol)
                        selected.update(refs if len(refs) <= SYMBOL_CAP else ALL)
                    elif any(CALL_RE.search(line) for line in added):
                        selected.update(ALL)
                    else:
                        found = module_tests(path)
                        selected.update(found if found else ALL)
            else:
                for line in hunk["insert_at"]:
                    line = max(1, line - 1)
                    if is_cosmetic(added):
                        func = enclosing_func(path, line)
                        cls = enclosing_class(path, line)
                        if func:
                            selected.update(range_tests(path, func[0], func[1]))
                        elif cls:
                            selected.update(range_tests(path, cls[0], cls[1]))
                        elif pos_sensitive(path):
                            selected.update(module_tests(path))
                        continue
                    if any(DEF_RE.match(item) or IMPORT_RE.match(item) for item in added):
                        selected.update(ALL)
                        continue
                    func = enclosing_func(path, line)
                    assign = enclosing_class_assign(path, line)
                    cls = enclosing_class(path, line)
                    if func:
                        selected.update(range_tests(path, func[0], func[1]))
                    elif assign:
                        _, _, symbol, class_start, class_end = assign
                        selected.update(class_assign_selection(path, symbol, class_start, class_end))
                    elif cls:
                        symbol = assign_symbol(added)
                        if symbol:
                            selected.update(class_assign_selection(path, symbol, cls[0], cls[1]))
                        else:
                            selected.update(range_tests(path, cls[0], cls[1]))
                    else:
                        if any(CALL_RE.search(item) for item in added):
                            selected.update(ALL)
                        else:
                            symbol = assign_symbol(added)
                            found = module_tests(path)
                            selected.update(found if found else ALL)
                            if symbol:
                                refs = symbol_tests(symbol)
                                selected.update(refs if len(refs) <= SYMBOL_CAP else ALL)
    return selected


def schedule(numbers):
    numbers = [number for number in numbers if number in TESTS_BY_NUMBER]
    if not numbers:
        return []
    ordered = sorted(numbers, key=lambda number: TESTS_BY_NUMBER[number]["seconds"], reverse=True)
    best = None
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        loads = [0.0] * count
        workers = [[] for _ in range(count)]
        for number in ordered:
            index = loads.index(min(loads))
            workers[index].append(number)
            loads[index] += TESTS_BY_NUMBER[number]["seconds"]
        cost = max(loads) + 2 * count
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main():
    commits = sorted((DATA / "commits").glob("*.patch"))
    for patch in commits:
        files = parse_patch(patch)
        workers = schedule(select(files))
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"answered {len(commits)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
