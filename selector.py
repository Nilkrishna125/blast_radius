#!/usr/bin/env python3
"""Blast Radius selector: pick the tests a patch can break and split them over workers.

Maps (all built from the pinned sqlglot commit, see tools/):

* map/inv.json.gz       line coverage from one shared pytest run (fast, but import-time
                        lines are credited only to the first test that ran them).
* map/run.json.gz       per-test isolated coverage of lines executed inside function code.
                        Each test ran in its own process, so functions run at import time,
                        cached functions and setUpClass are credited to every dependent test.
* map/classes.json      per class (path:qualname): tests that instantiated it or a subclass,
                        or called its classmethods. Used for class-body (attribute) changes.
* map/fixtures.json     fixture stem -> tests that use its content.
* map/fixture_readers.json  fixture stem -> every test that opens the file (traced).

Selection:

* A changed line inside a function body selects the tests that ran its statement
  (shared map | isolated map), not the whole function.
* A change inside a class body adds every test that used that class or a subclass.
* A file whose AST is unchanged by the patch (comments, blank lines) selects nothing.
* Module/class-level symbols, deletions, renames, syntax errors etc. keep the earlier
  conservative rules (symbol references, dialect tests, or all tests).
* Every commit is answered. If one commit raises, or the time budget runs out, that
  commit gets a conservative answer instead of the run losing it.
"""

import ast
import bisect
import gc
import gzip
import heapq
import json
import math
import mmap
import os
import pickle
import re
import sys
import time
import warnings
import zlib
from pathlib import Path

warnings.simplefilter("ignore")
START = time.monotonic()

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
BASE = Path(os.environ.get("BLAST_BASE", "/app/base"))
MAP = Path(os.environ.get("BLAST_MAP", "/app/map"))
MAX_WORKERS = 8
WORKER_COST_MS = 2000
SYMBOL_CAP = 600
# The run limit is 180 s (270 s in the final). Past this many seconds every remaining
# commit gets the fast conservative answer, so the batch always completes.
BUDGET_SECONDS = float(os.environ.get("BLAST_BUDGET", "110"))

INDEX_VERSION = 1
MAGIC = b"BRIDX1\0\0"
PYVER = [sys.version_info[0], sys.version_info[1]]

HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
COND_RE = re.compile(r"(\bif\b|\belif\b|\bwhile\b|\band\b|\bor\b|\bnot\b|==|!=|<=|>=|\bis\b|\bin\b)")
DEF_RE = re.compile(r"\s*(async\s+def|def|class)\b")
IMPORT_RE = re.compile(r"\s*(import|from)\b")
CALL_RE = re.compile(r"\(")
ASSIGN_RE = re.compile(r"\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=")
POS_RE = re.compile(r"\b(lineno|col_offset|end_lineno|end_col_offset|token_line)\b")
# The boundaries str.splitlines() uses, so hit offsets map to the same line numbers.
SPLIT_RE = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
POSITION_FILES = {
    "sqlglot/tokens.py",
    "sqlglot/tokenizer_core.py",
    "sqlglot/parser.py",
    "sqlglot/errors.py",
}

if hasattr(int, "bit_count"):
    def popcount(value):
        return value.bit_count()
else:  # Python < 3.10
    def popcount(value):
        return bin(value).count("1")


# ----------------------------------------------------------------------------
# bitmask helpers
# ----------------------------------------------------------------------------

def to_mask(numbers):
    nums = [n for n in numbers if n >= 0]
    if not nums:
        return 0
    buf = bytearray(max(nums) // 8 + 1)
    for n in nums:
        buf[n >> 3] |= 1 << (n & 7)
    return int.from_bytes(buf, "little")


def mask_to_numbers(mask):
    if mask <= 0:
        return []
    digits = bin(mask)[:1:-1]
    return [i for i, c in enumerate(digits) if c == "1"]


# ----------------------------------------------------------------------------
# source files, AST blocks
# ----------------------------------------------------------------------------

def decode_source(raw):
    """Same text Path.read_text(errors="replace") gives (universal newlines)."""
    text = raw.decode("utf-8", "replace")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def compute_blocks(text):
    """(funcs, classes, class_assigns, string_spans) of one module, or None if it does not parse."""
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    funcs, classes, class_assigns, strings = [], [], [], []
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
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = getattr(node, "lineno", 0)
            e = getattr(node, "end_lineno", s)
            if s and e and e > s:
                strings.append((s, e))
    return funcs, classes, class_assigns, strings


def _paint(items, smallest):
    """arr[line] = index of the enclosing item: the smallest span (ties: first) or the largest."""
    if not items:
        return [-1]
    arr = [-1] * (max(item[1] for item in items) + 2)
    if smallest:
        order = sorted(range(len(items)), key=lambda i: (items[i][1] - items[i][0], i), reverse=True)
    else:
        order = sorted(range(len(items)), key=lambda i: (items[i][1] - items[i][0], -i))
    for i in order:
        s, e = items[i][0], items[i][1]
        if s < 1 or e < s:
            continue
        arr[s:e + 1] = [i] * (e - s + 1)
    return arr


class Blocks:
    __slots__ = ("funcs", "classes", "assigns", "strings", "_f", "_c", "_a")

    def __init__(self, funcs, classes, assigns, strings):
        self.funcs = funcs
        self.classes = classes
        self.assigns = assigns
        self.strings = strings
        self._f = self._c = self._a = None

    def func_at(self, line):
        if self._f is None:
            self._f = _paint(self.funcs, True)
        arr = self._f
        if 1 <= line < len(arr):
            idx = arr[line]
            if idx >= 0:
                return self.funcs[idx]
        return None

    def class_at(self, line):
        if self._c is None:
            self._c = _paint(self.classes, False)
        arr = self._c
        if 1 <= line < len(arr):
            idx = arr[line]
            if idx >= 0:
                return self.classes[idx]
        return None

    def assign_at(self, line):
        if self._a is None:
            self._a = _paint(self.assigns, True)
        arr = self._a
        if 1 <= line < len(arr):
            idx = arr[line]
            if idx >= 0:
                return self.assigns[idx]
        return None


# ----------------------------------------------------------------------------
# coverage map (per source file)
# ----------------------------------------------------------------------------

def build_file_inv(linemap):
    """{"12": [tests], ...} -> (sorted lines, mask id per line, distinct masks, union mask)."""
    items = sorted((int(key), value) for key, value in linemap.items())
    lines, ids, masks = [], [], []
    seen = {}
    prev_list, prev_id = None, -1
    module = 0
    for line, numbers in items:
        if prev_list is not None and numbers == prev_list:
            mask_id = prev_id
        else:
            key = tuple(numbers)
            mask_id = seen.get(key)
            if mask_id is None:
                mask_id = len(masks)
                seen[key] = mask_id
                mask = to_mask(numbers)
                masks.append(mask)
                module |= mask
        prev_list, prev_id = numbers, mask_id
        lines.append(line)
        ids.append(mask_id)
    return lines, ids, masks, module


class FileInv:
    __slots__ = ("lines", "ids", "masks", "module", "_range")

    def __init__(self, lines, ids, masks, module):
        self.lines = lines
        self.ids = ids
        self.masks = masks
        self.module = module
        self._range = {}

    def line_mask(self, line):
        lines = self.lines
        i = bisect.bisect_left(lines, line)
        if i < len(lines) and lines[i] == line:
            return self.masks[self.ids[i]]
        return 0

    def range_mask(self, low, high):
        key = (low, high)
        found = self._range.get(key)
        if found is None:
            lo = bisect.bisect_left(self.lines, low)
            hi = bisect.bisect_right(self.lines, high)
            found = 0
            if hi > lo:
                masks = self.masks
                for j in set(self.ids[lo:hi]):
                    found |= masks[j]
            self._range[key] = found
        return found


class IndexFile:
    """map/index.bin: a small JSON header, then one zlib-compressed pickle per entry."""

    def __init__(self, path):
        self.mm = None
        handle = open(path, "rb")
        try:
            self.mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        finally:
            handle.close()
        if self.mm[:8] != MAGIC:
            raise ValueError("bad magic")
        header_len = int.from_bytes(self.mm[8:16], "little")
        self.header = json.loads(self.mm[16:16 + header_len])
        self.data_start = 16 + header_len
        if self.header.get("version") != INDEX_VERSION:
            raise ValueError("index version")

    def get(self, section, path):
        spot = self.header.get(section, {}).get(path)
        if spot is None:
            return None
        start = self.data_start + spot[0]
        return pickle.loads(zlib.decompress(self.mm[start:start + spot[1]]))


# ----------------------------------------------------------------------------
# state, loaded once by init()
# ----------------------------------------------------------------------------

DUR_MS = {}
ALL = 0
TEST_FILES = set()
FILE_MASK = {}
METHOD_MASK = {}
FIXTURES = {}
_FIXTURE_MASK = {}
_INDEX = None
_RAW_INV = None
_INV = {}
_MODULE = {}
_RAW = {}
_TEXT = {}
_LINES = {}
_BLOCKS = {}
_SYMBOLS = {}
_DIALECTS = {}
_POSITIVE = {}
_SCAN = None
_STARTS = {}


def init():
    global DUR_MS, ALL, TEST_FILES, FILE_MASK, METHOD_MASK, FIXTURES, _INDEX
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    DUR_MS = {}
    by_file, by_method = {}, {}
    for test in tests:
        number = test["number"]
        DUR_MS[number] = math.floor(test["seconds"] * 1000 + 0.5)
        by_file.setdefault(test["file"], []).append(number)
        parts = test["id"].split("::")
        if len(parts) >= 2:
            by_method.setdefault((parts[0], parts[-1].split("[")[0]), []).append(number)
    ALL = to_mask(DUR_MS)
    TEST_FILES = set(by_file)
    FILE_MASK = {key: to_mask(value) for key, value in by_file.items()}
    METHOD_MASK = {key: to_mask(value) for key, value in by_method.items()}
    try:
        FIXTURES = json.loads((MAP / "fixtures.json").read_text())
    except Exception:
        FIXTURES = {}
    _INDEX = None
    index_path = MAP / "index.bin"
    if index_path.exists():
        try:
            candidate = IndexFile(index_path)
            inv_path = MAP / "inv.json.gz"
            if inv_path.exists():
                crc = zlib.crc32(inv_path.read_bytes())
                if candidate.header.get("crc_inv") != crc:
                    raise ValueError("index does not match inv.json.gz")
            _INDEX = candidate
        except Exception as error:
            print(f"index ignored: {error}", file=sys.stderr)
            _INDEX = None


def fixture_mask(stem):
    """Tests for a fixture stem, or None when the stem is unknown."""
    if stem in _FIXTURE_MASK:
        return _FIXTURE_MASK[stem]
    value = FIXTURES.get(stem)
    mask = None if value is None else to_mask(value)
    _FIXTURE_MASK[stem] = mask
    return mask


_READERS = None


def fixture_reader_mask(stem):
    """Every test that opens a fixture with this stem (traced per test), or None."""
    global _READERS
    if _READERS is None:
        try:
            _READERS = json.loads((MAP / "fixture_readers.json").read_text())
        except Exception:
            _READERS = {}
    value = _READERS.get(stem)
    return None if value is None else to_mask(value)


def get_inv(path):
    if path in _INV:
        return _INV[path]
    inv = None
    try:
        if _INDEX is not None:
            entry = _INDEX.get("inv", path)
            if entry is not None:
                inv = FileInv(*entry)
        else:
            global _RAW_INV
            if _RAW_INV is None:
                try:
                    with gzip.open(MAP / "inv.json.gz", "rt") as handle:
                        _RAW_INV = json.load(handle)
                except Exception as error:
                    print(f"coverage map unavailable: {error}", file=sys.stderr)
                    _RAW_INV = {}
            raw = _RAW_INV.get(path)
            if raw:
                inv = FileInv(*build_file_inv(raw))
    except Exception as error:
        print(f"coverage entry failed for {path}: {error}", file=sys.stderr)
        inv = None
    _INV[path] = inv
    return inv


def line_mask(path, line):
    inv = get_inv(path)
    return inv.line_mask(line) if inv else 0


def range_mask(path, low, high):
    inv = get_inv(path)
    return inv.range_mask(low, high) if inv else 0


def module_mask(path):
    value = _MODULE.get(path)
    if value is None:
        inv = get_inv(path)
        value = inv.module if inv else 0
        _MODULE[path] = value
    return value


def read_source(path):
    if path in _TEXT:
        return _TEXT[path]
    try:
        raw = (BASE / path).read_bytes()
    except Exception:
        _TEXT[path] = None
        return None
    _RAW[path] = raw
    _TEXT[path] = decode_source(raw)
    return _TEXT[path]


def source_lines(path):
    lines = _LINES.get(path)
    if lines is None:
        text = read_source(path)
        lines = text.splitlines() if text is not None else []
        _LINES[path] = lines
    return lines


def get_blocks(path):
    if path in _BLOCKS:
        return _BLOCKS[path]
    result = None
    text = read_source(path)
    if text is not None:
        data = None
        found = False
        if _INDEX is not None:
            try:
                entry = _INDEX.get("blk", path)
                if entry is not None and entry[0] == zlib.crc32(_RAW[path]) and entry[1] == PYVER:
                    data = entry[2]
                    found = True
            except Exception:
                found = False
        if not found:
            data = compute_blocks(text)
        if data is not None:
            result = Blocks(*data)
    _BLOCKS[path] = result
    return result


def enclosing_func(path, line):
    info = get_blocks(path)
    return info.func_at(line) if info else None


def enclosing_class(path, line):
    info = get_blocks(path)
    return info.class_at(line) if info else None


def enclosing_class_assign(path, line):
    info = get_blocks(path)
    return info.assign_at(line) if info else None


def is_cosmetic(path, lines, insert_line):
    if not all((not line.strip()) or line.strip().startswith("#") for line in lines):
        return False
    info = get_blocks(path)
    spans = info.strings if info else []
    return not any(start <= insert_line < end for start, end in spans)


def pos_sensitive(path):
    if path in _POSITIVE:
        return _POSITIVE[path]
    if path in POSITION_FILES:
        value = True
    else:
        text = read_source(path)
        value = True if text is None else bool(POS_RE.search(text))
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


def is_dialect_file(path):
    return any(part in path for part in ("/dialects/", "/parsers/", "/generators/", "/typing/"))


def dialect_tests(path):
    stem = os.path.splitext(path.split("/")[-1])[0]
    if stem in _DIALECTS:
        return _DIALECTS[stem]
    found = 0
    for candidate in (
        f"sqlglot/dialects/{stem}.py",
        f"sqlglot/parsers/{stem}.py",
        f"sqlglot/generators/{stem}.py",
        f"sqlglot/typing/{stem}.py",
    ):
        found |= module_mask(candidate)
    _DIALECTS[stem] = found
    return found


def scan_files():
    global _SCAN
    if _SCAN is None:
        found = []
        for base in (BASE / "sqlglot", BASE / "tests"):
            if not base.exists():
                continue
            for root, _, names in os.walk(base):
                for name in names:
                    if not name.endswith(".py"):
                        continue
                    path = str((Path(root) / name).relative_to(BASE))
                    if path.startswith("tests/") and path not in TEST_FILES:
                        continue
                    found.append(path)
        found.sort()
        _SCAN = found
    return _SCAN


def symbol_tests(symbol):
    """Tests reaching any line that mentions `symbol` as a whole word."""
    if symbol in _SYMBOLS:
        return _SYMBOLS[symbol]
    found = 0
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    for path in scan_files():
        text = read_source(path)
        if not text or symbol not in text:
            continue
        starts = _STARTS.get(path)
        if starts is None:
            starts = [0]
            starts.extend(m.end() for m in SPLIT_RE.finditer(text))
            _STARTS[path] = starts
        hit_lines = set()
        for match in pattern.finditer(text):
            hit_lines.add(bisect.bisect_right(starts, match.start()))
        if not hit_lines:
            continue
        in_tests = path.startswith("tests/")
        funcs_done = set()
        for index in sorted(hit_lines):
            func = enclosing_func(path, index)
            if in_tests:
                if func and func[3].startswith("test"):
                    found |= METHOD_MASK.get((path, func[3]), 0)
                else:
                    found |= FILE_MASK.get(path, 0)
            elif func:
                if func not in funcs_done:
                    funcs_done.add(func)
                    found |= range_mask(path, func[0], func[1])
            else:
                found |= line_mask(path, index)
    _SYMBOLS[symbol] = found
    return found


def class_assign_selection(path, symbol, class_start, class_end):
    if is_dialect_file(path):
        refs = symbol_tests(symbol)
        if popcount(refs) <= SYMBOL_CAP:
            return range_mask(path, class_start, class_end) | refs
        return dialect_tests(path)
    selected = module_mask(path)
    refs = symbol_tests(symbol)
    if popcount(refs) <= SYMBOL_CAP:
        selected |= refs
    else:
        cls = enclosing_class(path, class_start)
        if cls:
            selected |= symbol_tests(cls[2])
    # Kept on purpose: the previous version updated its cached per-module set in place
    # here, so later commits in the same run saw these tests too. Same behaviour, only wider.
    _MODULE[path] = selected
    if not selected:
        selected = range_mask(path, class_start, class_end)
    return selected or ALL


# ----------------------------------------------------------------------------
# patch parsing
# ----------------------------------------------------------------------------

def _new_meta(new=False, deleted=False):
    return {"new": new, "deleted": deleted, "rename": False, "binary": False, "hunks": []}


def parse_patch(path):
    """Unified diff -> {path: {new, deleted, rename, binary, hunks[...]}}.

    Hunk bodies are read by the line counts in the @@ header, so body lines that look
    like file headers (`--- text` from a removed `-- text`) stay body lines.
    """
    text = path.read_bytes().decode("utf-8", "replace")
    lines = text.split("\n")
    total = len(lines)
    files = {}
    cur = None
    hunk = None
    old_line = 0
    old_path = None
    prev_plus = False
    rem_old = rem_new = 0
    for i in range(total):
        raw = lines[i]
        if raw.endswith("\r"):
            raw = raw[:-1]
        if hunk is not None:
            tag = raw[:1]
            if tag == "\\":
                continue
            if rem_old > 0 or rem_new > 0:
                is_body = tag in ("+", "-", " ", "")
            else:
                header_like = raw.startswith("--- ") and i + 1 < total and lines[i + 1].startswith("+++ ")
                is_body = tag in ("+", "-", " ") and not header_like
            if is_body:
                body = raw[1:]
                if tag == "+":
                    hunk["added"].append(body)
                    hunk["seq"].append(("+", body))
                    if not prev_plus:
                        hunk["insert_at"].append(old_line)
                    prev_plus = True
                    rem_new -= 1
                elif tag == "-":
                    hunk["changed_old"].append(old_line)
                    hunk["seq"].append(("-", body))
                    old_line += 1
                    prev_plus = False
                    rem_old -= 1
                else:
                    hunk["seq"].append((" ", body))
                    old_line += 1
                    prev_plus = False
                    rem_old -= 1
                    rem_new -= 1
                continue
            hunk = None
            rem_old = rem_new = 0
        if raw.startswith("diff --git "):
            parts = raw.split(" ")
            if len(parts) >= 4:
                left = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                right = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                cur = right
                files.setdefault(cur, _new_meta())
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
                meta = _new_meta(deleted=True)
            elif old_path == "/dev/null":
                cur = new_path
                meta = _new_meta(new=True)
            else:
                cur = new_path
                meta = _new_meta()
            previous = files.get(cur)
            if previous:  # keep flags set by earlier header lines (rename, binary)
                meta["rename"] = previous["rename"]
                meta["binary"] = previous["binary"]
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
                rem_old = hunk["old_count"]
                rem_new = hunk["new_count"]
                prev_plus = False
        elif raw.startswith("rename ") or raw.startswith("copy "):
            if cur:
                files[cur]["rename"] = True
        elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            if cur:
                files[cur]["binary"] = True
    return files


def patched_text(base_text, hunks):
    lines = base_text.splitlines()
    for hunk in sorted(hunks, key=lambda item: item["old_start"], reverse=True):
        index = hunk["old_start"] if hunk["old_count"] == 0 else hunk["old_start"] - 1
        segment = [text for tag, text in hunk["seq"] if tag != "-"]
        lines[index:index + hunk["old_count"]] = segment
    return "\n".join(lines)


def syntax_ok(path, hunks):
    text = read_source(path)
    if text is None:
        return False
    try:
        compile(patched_text(text, hunks), "<patched>", "exec", ast.PyCF_ONLY_AST)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------------

STMT_MODE = os.environ.get("STMT_MODE", "1") == "1"
AST_EQ_MODE = os.environ.get("AST_EQ_MODE", "1") == "1"
_STMTS = {}


def _stmt_index(path):
    """Sorted (start, end) spans: simple statements, and the header part of compound ones."""
    if path in _STMTS:
        return _STMTS[path]
    spans = []
    text = read_source(path)
    try:
        tree = ast.parse(text)
    except Exception:
        _STMTS[path] = None
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end = node.end_lineno
        bodies = [getattr(node, f, None) for f in ("body", "orelse", "finalbody", "handlers")]
        firsts = [b[0].lineno for b in bodies if isinstance(b, list) and b]
        if isinstance(node, ast.Match):
            firsts += [c.pattern.lineno for c in node.cases]
        if firsts and isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)) :
            end = max(node.lineno, min(firsts) - 1)
            # elif / else-if chains: an If inside orelse is its own node
        spans.append((node.lineno, end, node.end_lineno - node.lineno))
    _STMTS[path] = spans
    return spans


def stmt_span(path, line):
    """Smallest statement (or compound-statement header) containing `line`."""
    spans = _stmt_index(path)
    if not spans:
        return None
    best = None
    for s, e, size in spans:
        if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e)
    return best


def ast_equivalent(path, hunks):
    text = read_source(path)
    try:
        a = ast.dump(ast.parse(text))
        b = ast.dump(ast.parse(patched_text(text, hunks)))
    except Exception:
        return False
    return a == b


CLASS_MODE = os.environ.get("CLASS_MODE", "1") == "1"
RUN_MODE = os.environ.get("RUN_MODE", "1") == "1"
_RUNMAP = None
_CLASSMAP = None
_QUAL = {}


def _run_file(path):
    global _RUNMAP
    if _RUNMAP is None:
        try:
            with gzip.open(MAP / "run.json.gz", "rt") as handle:
                _RUNMAP = json.load(handle)
        except Exception:
            _RUNMAP = {}
    return _RUNMAP.get(path, {})


def run_range_mask(path, low, high):
    """Tests that executed lines low..high inside function code (isolated per-test run)."""
    found = 0
    for key, numbers in _run_file(path).items():
        if low <= int(key) <= high:
            found |= to_mask(numbers)
    return found


def class_usage(path, line):
    """Tests that used the innermost class around `line` or any subclass of it."""
    global _CLASSMAP
    if _CLASSMAP is None:
        try:
            _CLASSMAP = json.loads((MAP / "classes.json").read_text())
        except Exception:
            _CLASSMAP = {}
    quals = _QUAL.get(path)
    if quals is None:
        quals = []
        try:
            tree = ast.parse(read_source(path))
            def visit(node, prefix):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        name = prefix + child.name
                        quals.append((child.lineno, child.end_lineno, name))
                        visit(child, name + ".")
                    elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        visit(child, prefix + child.name + ".<locals>.")
                    else:
                        visit(child, prefix)
            visit(tree, "")
        except Exception:
            pass
        _QUAL[path] = quals
    best = None
    for s, e, name in quals:
        if s <= line <= e and (best is None or e - s < best[1] - best[0]):
            best = (s, e, name)
    if best is None:
        return None
    value = _CLASSMAP.get(path + ":" + best[2])
    return to_mask(value) if value is not None else None


def escalate(path, anchor, chosen):
    func = enclosing_func(path, anchor)
    if func:
        chosen |= range_mask(path, func[0], func[1])
        if RUN_MODE:
            chosen |= run_range_mask(path, func[2], func[1])
    if not chosen:
        cls = enclosing_class(path, anchor)
        if cls:
            chosen |= range_mask(path, cls[0], cls[1])
    if not chosen:
        chosen |= module_mask(path)
    if not chosen:
        chosen |= ALL
    return chosen


def select_hunks(path, meta):
    selected = 0
    source = source_lines(path)

    def text(line):
        return source[line - 1] if 0 <= line - 1 < len(source) else ""

    for hunk in meta["hunks"]:
        added = hunk["added"]
        chosen = 0
        intentional = False
        if hunk["changed_old"]:
            control = any(COND_RE.search(line) for line in added)
            for line in hunk["changed_old"]:
                current = text(line)
                if DEF_RE.match(current) or IMPORT_RE.match(current):
                    chosen |= ALL
                    continue
                func = enclosing_func(path, line)
                if func:
                    span = stmt_span(path, line) if (STMT_MODE and line > func[2]) else None
                    if span is not None:
                        chosen |= range_mask(path, span[0], span[1])
                        if RUN_MODE:
                            chosen |= run_range_mask(path, span[0], span[1])
                    elif control or COND_RE.search(current) or line <= func[2]:
                        chosen |= range_mask(path, func[0], func[1])
                    else:
                        chosen |= line_mask(path, line)
                    continue
                assign = enclosing_class_assign(path, line)
                if CLASS_MODE and enclosing_class(path, line):
                    usage = class_usage(path, line)
                    if usage is not None:
                        chosen |= usage
                if assign:
                    _, _, symbol, class_start, class_end = assign
                    chosen |= class_assign_selection(path, symbol, class_start, class_end)
                    continue
                cls = enclosing_class(path, line)
                if cls:
                    chosen |= range_mask(path, cls[0], cls[1])
                    continue
                symbol = None
                match = ASSIGN_RE.match(current)
                if match:
                    symbol = match.group(1)
                if symbol is None:
                    symbol = assign_symbol(added)
                if any(CALL_RE.search(item) for item in added):
                    chosen |= ALL
                elif symbol:
                    found = module_mask(path)
                    if found:
                        chosen |= found
                    elif is_dialect_file(path):
                        chosen |= dialect_tests(path)
                    else:
                        chosen |= ALL
                    refs = symbol_tests(symbol)
                    chosen |= refs if popcount(refs) <= SYMBOL_CAP else ALL
                    if is_dialect_file(path):
                        chosen |= dialect_tests(path)
                else:
                    found = module_mask(path)
                    chosen |= found if found else ALL
        else:
            runs = hunk["insert_at"]
            cosmetic = bool(runs) and all(is_cosmetic(path, added, item) for item in runs)
            anchor0 = max(1, runs[0] - 1) if runs else 1
            module_level = not enclosing_func(path, anchor0) and not enclosing_class(path, anchor0)
            intentional = cosmetic and module_level and not pos_sensitive(path)
            for insert_line in runs:
                line = max(1, insert_line - 1)
                if cosmetic:
                    func = enclosing_func(path, line)
                    cls = enclosing_class(path, line)
                    if func:
                        chosen |= range_mask(path, func[0], func[1])
                    elif cls:
                        chosen |= range_mask(path, cls[0], cls[1])
                    elif pos_sensitive(path):
                        found = module_mask(path)
                        chosen |= found if found else ALL
                    continue
                if any(DEF_RE.match(item) or IMPORT_RE.match(item) for item in added):
                    chosen |= ALL
                    continue
                func = enclosing_func(path, line)
                assign = enclosing_class_assign(path, line)
                cls = enclosing_class(path, line)
                if CLASS_MODE and not func and cls:
                    usage = class_usage(path, line)
                    if usage is not None:
                        chosen |= usage
                if func and STMT_MODE and line >= func[2] and enclosing_func(path, insert_line) == func:
                    for anchor in (line, insert_line):
                        span = stmt_span(path, anchor)
                        if span is not None:
                            chosen |= range_mask(path, span[0], span[1])
                            if RUN_MODE:
                                chosen |= run_range_mask(path, span[0], span[1])
                    if not chosen:
                        chosen |= range_mask(path, func[0], func[1])
                elif func:
                    chosen |= range_mask(path, func[0], func[1])
                elif assign:
                    _, _, symbol, class_start, class_end = assign
                    chosen |= class_assign_selection(path, symbol, class_start, class_end)
                elif cls:
                    symbol = assign_symbol(added)
                    if symbol:
                        chosen |= class_assign_selection(path, symbol, cls[0], cls[1])
                    else:
                        chosen |= range_mask(path, cls[0], cls[1])
                else:
                    if any(CALL_RE.search(item) for item in added):
                        chosen |= ALL
                    else:
                        symbol = assign_symbol(added)
                        found = module_mask(path)
                        chosen |= found if found else ALL
                        if symbol:
                            refs = symbol_tests(symbol)
                            chosen |= refs if popcount(refs) <= SYMBOL_CAP else ALL
        if not chosen and not intentional:
            anchor = hunk["changed_old"][0] if hunk["changed_old"] else max(1, (hunk["insert_at"][0] if hunk["insert_at"] else 1))
            chosen = escalate(path, anchor, chosen)
        selected |= chosen
    return selected


def select(files):
    selected = 0
    for path, meta in files.items():
        if meta["rename"] or meta["binary"]:
            selected |= ALL
            continue
        if meta["deleted"]:
            if path.startswith("tests/fixtures/"):
                stem = os.path.splitext(os.path.basename(path))[0]
                mask = fixture_mask(stem)
                selected |= ALL if mask is None else mask
            elif path.startswith("tests/") and path.endswith(".py") and path in TEST_FILES:
                selected |= FILE_MASK.get(path, 0)
            else:
                selected |= ALL
            continue
        if meta["new"]:
            if path.startswith("tests/fixtures/"):
                continue
            if path.startswith("tests/") and path.endswith(".py"):
                if is_helper(path):
                    selected |= ALL
                continue
            if path.endswith(".py") and path.startswith("sqlglot/"):
                continue
            if path.endswith(".py"):
                selected |= ALL
            continue
        if path.startswith("tests/") and path.endswith(".py"):
            if path in TEST_FILES:
                selected |= FILE_MASK.get(path, 0)
            elif is_helper(path):
                selected |= ALL
            continue
        if path.startswith("tests/"):
            stem = os.path.splitext(os.path.basename(path))[0]
            mask = fixture_mask(stem)
            selected |= ALL if mask is None else mask
            # A "# key: value" meta line is parsed while the fixture loads, so a broken one
            # can fail every test that reads the file (including setUpClass readers).
            if any(text.lstrip().startswith("#") for hunk in meta["hunks"] for tag, text in hunk["seq"] if tag != " "):
                readers = fixture_reader_mask(stem)
                selected |= ALL if readers is None else readers
            continue
        if not path.endswith(".py"):
            if path.endswith("py.typed"):
                continue
            found = module_mask(path)
            selected |= found if found else ALL
            continue
        if path == "sqlglot/__init__.py":
            selected |= ALL
            continue
        if not syntax_ok(path, meta["hunks"]):
            selected |= ALL
            continue
        if AST_EQ_MODE and ast_equivalent(path, meta["hunks"]):
            continue
        if get_blocks(path) is None:
            selected |= ALL
            continue
        selected |= select_hunks(path, meta)
    return selected


def conservative(files):
    """Cheap, wide answer used if the full analysis fails or time runs out."""
    selected = 0
    for path, meta in files.items():
        if meta["rename"] or meta["binary"] or meta["deleted"] or meta["new"]:
            selected |= ALL
        elif path.startswith("tests/fixtures/") or (path.startswith("tests/") and not path.endswith(".py")):
            mask = fixture_mask(os.path.splitext(os.path.basename(path))[0])
            selected |= ALL if mask is None else mask
        elif path.startswith("tests/") and path.endswith(".py"):
            selected |= FILE_MASK.get(path, 0) if path in TEST_FILES else ALL
        elif path.endswith(".py") and path.startswith("sqlglot/") and path != "sqlglot/__init__.py":
            found = module_mask(path)
            selected |= found if found else ALL
        elif path.endswith("py.typed"):
            continue
        else:
            selected |= ALL
    return selected


# ----------------------------------------------------------------------------
# scheduling
# ----------------------------------------------------------------------------

_SCHEDULES = {}


def schedule(mask):
    """Greedy longest-first packing; picks the worker count with the lowest simulated cost."""
    mask &= ALL
    cached = _SCHEDULES.get(mask)
    if cached is not None:
        return [list(worker) for worker in cached]
    numbers = [n for n in mask_to_numbers(mask) if n in DUR_MS]
    if not numbers:
        _SCHEDULES[mask] = []
        return []
    numbers.sort(key=lambda n: DUR_MS[n], reverse=True)
    total = sum(DUR_MS[n] for n in numbers)
    longest = DUR_MS[numbers[0]]
    best_cost = None
    best_workers = None
    # Highest count first: for big selections its cost is the lowest, so the bound below
    # then skips every smaller count without packing it.
    for count in range(min(MAX_WORKERS, len(numbers)), 0, -1):
        bound = max(longest, -(-total // count)) + WORKER_COST_MS * count
        if best_cost is not None and bound >= best_cost:
            continue
        heap = [(0, j) for j in range(count)]
        workers = [[] for _ in range(count)]
        for number in numbers:
            load, j = heapq.heappop(heap)
            workers[j].append(number)
            heapq.heappush(heap, (load + DUR_MS[number], j))
        cost = max(load for load, _ in heap) + WORKER_COST_MS * count
        if best_cost is None or cost <= best_cost:
            best_cost, best_workers = cost, workers
    _SCHEDULES[mask] = best_workers
    return [list(worker) for worker in best_workers]


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def answer(patch):
    files = parse_patch(patch)
    if time.monotonic() - START > BUDGET_SECONDS:
        return conservative(files)
    try:
        return select(files)
    except Exception as error:
        print(f"{patch.name}: {error!r}; using conservative answer", file=sys.stderr)
        return conservative(files)


def main():
    gc.disable()
    patches = sorted((DATA / "commits").glob("*.patch"))
    try:
        init()
    except Exception as error:
        print(f"init failed: {error!r}", file=sys.stderr)
        raise
    for patch in patches:
        try:
            mask = answer(patch)
        except Exception as error:
            print(f"{patch.name}: {error!r}; running everything", file=sys.stderr)
            mask = ALL
        sys.stdout.write(json.dumps({"commit": patch.name[: -len(".patch")], "workers": schedule(mask)}) + "\n")
        sys.stdout.flush()
    print(f"answered {len(patches)} commits in {time.monotonic() - START:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()