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
ID_NUM = {}
NUM_ID = {}
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
    ID_NUM.clear()
    ID_NUM.update({test["id"]: test["number"] for test in tests})
    NUM_ID.clear()
    NUM_ID.update({test["number"]: test["id"] for test in tests})
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
    tree = base_tree(path)
    if tree is None:
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


_TREES = {}


def base_tree(path):
    """Parsed AST of the pinned source of `path` (cached), or None."""
    if path not in _TREES:
        try:
            _TREES[path] = ast.parse(read_source(path))
        except Exception:
            _TREES[path] = None
    return _TREES[path]


def ast_equivalent(path, hunks):
    # Only edits made of comments and blank lines can leave the AST unchanged in practice;
    # anything else is decided by the rules below without the (slow) full comparison.
    for hunk in hunks:
        for tag, line in hunk["seq"]:
            if tag != " " and line.strip() and not line.strip().startswith("#"):
                return False
    old = base_tree(path)
    if old is None:
        return False
    try:
        new = ast.parse(patched_text(read_source(path), hunks))
    except Exception:
        return False
    return ast.dump(old) == ast.dump(new)


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
    value = _RUNMAP.get(path, {})
    if isinstance(value, str):
        value = _RUNMAP[path] = json.loads(value)
    return value


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


ATTR_MODE = os.environ.get("ATTR_MODE", "1") == "1"
_ATTRS = None
_HOOK_NAMES = None


def _attrs():
    global _ATTRS
    if _ATTRS is None:
        try:
            with gzip.open(MAP / "attrs.json.gz", "rt") as handle:
                data = json.load(handle)
            data["import"] = set(data["import"])
            data["_masks"] = {}
            prefixes = {}
            for key in data["reads"]:
                parts = key.split("|")
                prefixes.setdefault(parts[0] + "|" + parts[1], []).append(key)
            data["_prefix"] = prefixes
            iprefixes = set()
            for key in data["import"]:
                parts = key.split("|")
                iprefixes.add(parts[0] + "|" + parts[1])
            data["_iprefix"] = iprefixes
            _ATTRS = data
        except Exception as error:
            print(f"attribute map unavailable: {error}", file=sys.stderr)
            _ATTRS = {}
    return _ATTRS


def _reads_mask(key):
    data = _attrs()
    cache = data["_masks"]
    if key not in cache:
        index = data["reads"].get(key)
        cache[key] = to_mask(data["sets"][index]) if index is not None else 0
    return cache[key]


def hook_names():
    """Attribute names a class-creation hook reads from the class being created (or from its
    nested tokenizer). Those reads happen before the module is instrumented, so they are not
    in the traced import reads. Expression.__init_subclass__ (arg_types -> required_args) is
    handled separately in attr_selection."""
    global _HOOK_NAMES
    if _HOOK_NAMES is None:
        names = set()
        own = {"cls", "klass", "mcs"}
        for path in scan_files():
            if not path.startswith("sqlglot/"):
                continue
            text = read_source(path)
            if not text or ("__init_subclass__" not in text and "(type)" not in text):
                continue
            try:
                tree = ast.parse(text)
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                is_meta = any(isinstance(b, ast.Name) and b.id == "type" for b in node.bases)
                for item in node.body:
                    if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if not (item.name == "__init_subclass__" or (is_meta and item.name in ("__new__", "__init__", "__call__"))):
                        continue
                    body_names = set()
                    for sub in ast.walk(item):
                        if isinstance(sub, ast.Attribute):
                            base = sub.value
                            if isinstance(base, ast.Name) and base.id in own:
                                body_names.add(sub.attr)
                            elif isinstance(base, ast.Attribute) and base.attr in ("tokenizer_class", "jsonpath_tokenizer_class"):
                                body_names.add(sub.attr)
                        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.isidentifier() and sub.value.isupper():
                            body_names.add(sub.value)
                    body_names.discard("arg_types")
                    names |= body_names
        _HOOK_NAMES = names
    return _HOOK_NAMES


def _key_reprs(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ["s:" + node.value]
    if isinstance(node, ast.Attribute):
        out = ["c:" + node.attr]
        base = node.value
        if isinstance(base, ast.Name):
            out.append("e:" + base.id + "." + node.attr)
        elif isinstance(base, ast.Attribute):
            out.append("e:" + base.attr + "." + node.attr)
        return out
    if isinstance(node, ast.Name):
        return ["c:" + node.id]
    return None


def _class_stmt(tree, line):
    """(qualname, statement) of the class-body statement holding `line`, or None."""
    def walk(body, prefix):
        for stmt in body:
            if not (stmt.lineno <= line <= stmt.end_lineno):
                continue
            if isinstance(stmt, ast.ClassDef):
                qual = prefix + stmt.name
                for inner in stmt.body:
                    if inner.lineno <= line <= inner.end_lineno:
                        if isinstance(inner, ast.ClassDef):
                            return walk([inner], qual + ".")
                        return (qual, inner)
                return None
            return None
        return None

    return walk(tree.body, "")


def _assign_target(stmt):
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id, stmt.value
    return None


def _same_file_refs(tree, name, stmt, qual):
    """Non-function code in this file, other than `stmt`, that loads this class's `name`:
    a bare `name` inside the same class body, or `<Class>.name` anywhere."""
    found = []
    simple = qual.split(".")[-1]

    def visit(node, cls_qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if child is stmt:
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, (cls_qual + "." if cls_qual else "") + child.name)
                continue
            if isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, ast.Load) and cls_qual == qual:
                found.append(child)
            elif isinstance(child, ast.Attribute) and child.attr == name and isinstance(child.ctx, ast.Load):
                base = child.value
                base_name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else None
                if base_name in (simple, "cls", "self") or base_name is None:
                    found.append(child)
            visit(child, cls_qual)

    visit(tree, "")
    return found


def _in_new_trie(tree, refs):
    """True if every reference sits inside a new_trie(...) call (which only reads keys)."""
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "new_trie"]
    for ref in refs:
        if not any(c.lineno <= ref.lineno <= c.end_lineno and c.col_offset <= ref.col_offset for c in calls):
            return False
    return True


def attr_selection(path, line, hunks):
    """Precise selection for a change to a class-body attribute, from traced attribute reads.

    Returns a test mask, or None when the change is not a plain class attribute (or the
    attribute feeds import-time work), in which case the caller uses the wider rules.
    """
    if enclosing_func(path, line):
        return None
    data = _attrs()
    if not data:
        return None
    text = read_source(path)
    old_tree = base_tree(path)
    if old_tree is None:
        return None
    try:
        new_tree = ast.parse(patched_text(text, hunks))
    except Exception:
        return None
    found = _class_stmt(old_tree, line)
    if not found:
        return None
    qual, stmt = found
    target = _assign_target(stmt)
    if not target:
        return None
    name, old_value = target
    if name.startswith("__") or name in hook_names():
        return None
    owner = path + ":" + qual
    # the same attribute in the patched file
    new_value = None
    for cand_qual, cand in _class_stmts(new_tree):
        if cand_qual == qual:
            t = _assign_target(cand)
            if t and t[0] == name:
                new_value = t[1]
                break
    if new_value is None:
        return None
    refs = _same_file_refs(old_tree, name, stmt, qual)
    value_only_keys = None
    if isinstance(old_value, ast.Dict) and isinstance(new_value, ast.Dict) and len(old_value.keys) == len(new_value.keys):
        keys_same = all(
            (a is None and b is None) or (a is not None and b is not None and ast.dump(a) == ast.dump(b))
            for a, b in zip(old_value.keys, new_value.keys)
        )
        if keys_same:
            changed = []
            for k, a, b in zip(old_value.keys, old_value.values, new_value.values):
                if ast.dump(a) != ast.dump(b):
                    if k is None:
                        changed = None
                        break
                    reprs = _key_reprs(k)
                    if reprs is None:
                        changed = None
                        break
                    changed.append(reprs)
            value_only_keys = changed
    if refs and not (value_only_keys is not None and _in_new_trie(old_tree, refs)):
        return None
    imports = data["import"]
    mask = 0
    if name == "arg_types":
        # Expression.__init_subclass__ derives required_args from arg_types in every subclass
        family = {owner} | set(data.get("subclasses", {}).get(owner, []))
        for o in family:
            if (o + "|required_args") in data["_iprefix"]:
                return None
            for key in data["_prefix"].get(o + "|required_args", []):
                mask |= _reads_mask(key)
        if value_only_keys is None:
            for o in family:
                if (o + "|arg_types") in data["_iprefix"]:
                    return None
                for key in data["_prefix"].get(o + "|arg_types", []):
                    mask |= _reads_mask(key)
            return mask
    if value_only_keys is None:
        # whole value changes: anything that read the attribute, unless import-time work read it
        if (owner + "|" + name) in data["_iprefix"]:
            return None
        for key in data["_prefix"].get(owner + "|" + name, []):
            mask |= _reads_mask(key)
        return mask
    for reprs in value_only_keys:
        owners = {owner}
        captured = False
        for r in reprs:
            for group in data["copies"].get(name + "|" + r, []):
                if owner in group:
                    owners.update(group)
                    captured = True
        for o in owners:
            if (o + "|" + name + "|*v") in imports and not captured:
                return None
            for r in reprs:
                if (o + "|" + name + "|" + r) in imports and not captured:
                    return None
                mask |= _reads_mask(o + "|" + name + "|" + r)
            mask |= _reads_mask(o + "|" + name + "|*v")
    return mask


def _class_stmts(tree):
    out = []

    def walk(body, prefix):
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                qual = prefix + stmt.name
                for inner in stmt.body:
                    if isinstance(inner, ast.ClassDef):
                        walk([inner], qual + ".")
                    else:
                        out.append((qual, inner))

    walk(tree.body, "")
    return out


EQ_MODE = os.environ.get("EQ_MODE", "1") == "1"
_EQ = None


def _eq_profile():
    global _EQ
    if _EQ is None:
        try:
            _EQ = json.loads((MAP / "eqprofile.json").read_text())
        except Exception:
            _EQ = {}
    return _EQ


def _swap_diff(a, b, sites):
    """True if trees a and b differ only by `is`->`==` / `is not`->`!=` in single comparisons."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for field in a._fields:
            va, vb = getattr(a, field, None), getattr(b, field, None)
            if field == "ops" and isinstance(a, ast.Compare):
                if len(va) != len(vb):
                    return False
                for oa, ob in zip(va, vb):
                    if type(oa) is type(ob):
                        continue
                    if len(va) == 1 and (
                        (isinstance(oa, ast.Is) and isinstance(ob, ast.Eq))
                        or (isinstance(oa, ast.IsNot) and isinstance(ob, ast.NotEq))
                        or (ORDER_SWAPS and _order_swap(oa, ob))
                    ):
                        sites.append((a.lineno, a.col_offset, type(oa).__name__))
                    else:
                        return False
                continue
            if not _swap_diff(va, vb, sites):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(_swap_diff(x, y, sites) for x, y in zip(a, b))
    return a == b


ORDER_SWAPS = False  # _swap_diff is shared: ordering swaps only count in swap_selection()


def _order_swap(oa, ob):
    pairs = ((ast.Lt, ast.LtE), (ast.LtE, ast.Lt), (ast.Gt, ast.GtE), (ast.GtE, ast.Gt))
    return any(isinstance(oa, x) and isinstance(ob, y) for x, y in pairs)


CMP_MODE = os.environ.get("CMP_MODE", "1") == "1"
_CMP = None


def swap_selection(path, hunks):
    """For a patch that only swaps `is`/`==`, `is not`/`!=`, `<`/`<=` or `>`/`>=` at single
    comparisons: the tests in which the swapped operator ever answered differently (per-test
    comparison profile). None when the patch is something else or a site is not profiled."""
    global _CMP, ORDER_SWAPS
    if _CMP is None:
        try:
            _CMP = json.loads((MAP / "cmpprofile.json").read_text())
        except Exception:
            _CMP = {}
    if not _CMP:
        return None
    old = base_tree(path)
    if old is None:
        return None
    try:
        new = ast.parse(patched_text(read_source(path), hunks))
    except Exception:
        return None
    sites = []
    ORDER_SWAPS = True
    try:
        same = _swap_diff(old, new, sites)
    finally:
        ORDER_SWAPS = False
    if not same or not sites:
        return None
    mask = 0
    for line, col, _ in sites:
        entry = _CMP.get(f"{path}:{line}:{col}")
        if entry == "*":
            return None
        if entry:
            mask |= to_mask(entry)
    return mask


def identity_swap_equivalent(path, hunks):
    """A patch that only turns identity comparisons into equality comparisons, at sites where
    the equality-semantics profile saw `is` and `==` agree on every evaluation, cannot change
    any execution the tests make."""
    profile = _eq_profile()
    if not profile:
        return False
    old = base_tree(path)
    if old is None:
        return False
    try:
        new = ast.parse(patched_text(read_source(path), hunks))
    except Exception:
        return False
    sites = []
    if not _swap_diff(old, new, sites) or not sites:
        return False
    for line, col, _ in sites:
        entry = profile.get(f"{path}:{line}:{col}")
        if entry is None:
            if line_mask(path, line) or run_range_mask(path, line, line):
                return False
        elif entry[1]:
            return False
    return True



_OWNERS = None


def _traced_owners():
    global _OWNERS
    if _OWNERS is None:
        data = _attrs()
        _OWNERS = {k.split("|", 1)[0] for k in data.get("reads", {})} if data else set()
    return _OWNERS


def _class_mask(owner):
    global _CLASSMAP
    if _CLASSMAP is None:
        try:
            _CLASSMAP = json.loads((MAP / "classes.json").read_text())
        except Exception:
            _CLASSMAP = {}
    value = _CLASSMAP.get(owner)
    return to_mask(value) if value else 0


def _function_body(path, qual, name):
    """(body_start, end) of def `name` in class `qual` (or at module level), or None."""
    tree = base_tree(path)
    if tree is None:
        return None
    body = tree.body
    if qual:
        for part in qual.split("."):
            nxt = None
            for node in body:
                if isinstance(node, ast.ClassDef) and node.name == part:
                    nxt = node.body
                    break
            if nxt is None:
                return None
            body = nxt
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name and node.body:
            return node.body[0].lineno, node.end_lineno
    return None


def diff_selection(info):
    """Tests that can observe the import-time state a patch changes (see state.py)."""
    data = _attrs()
    changed = info["changed"]
    added, removed = set(info["added"]), set(info["removed"])
    keyed = {k.rsplit("|", 1)[0] for k in changed if k.count("|") >= 2}
    owners = _traced_owners()
    mask = 0
    for key in changed:
        owner, rest = key.split("|", 1)
        parts = rest.split("|", 1)
        attr, sub = parts[0], (parts[1] if len(parts) > 1 else None)
        if ":" in owner:
            path, qual = owner.split(":", 1)
            base_owner = owner.split(".<", 1)[0]
            if attr == "__methods__":
                mask |= _class_mask(base_owner) | symbol_tests(qual.split(".")[-1])
                continue
            if sub == "__defaults__":
                span = _function_body(path, qual, attr) if ".<" not in owner else None
                if span:
                    mask |= run_range_mask(path, *span) | range_mask(path, *span)
                else:
                    mask |= _class_mask(base_owner)
                continue
            if ".<" in owner or owner not in owners:
                # not instrumented (dialect-built classes, enums): every user of the class
                mask |= _class_mask(base_owner) | symbol_tests(attr)
                continue
            if sub is not None:
                mask |= _reads_mask(owner + "|" + attr + "|" + sub) | _reads_mask(owner + "|" + attr + "|*v")
                if key in added or key in removed:
                    mask |= _reads_mask(owner + "|" + attr + "|*k")
                continue
            mask |= _reads_mask(owner + "|" + attr)
            if (owner + "|" + attr) not in keyed:
                for k in data["_prefix"].get(owner + "|" + attr, []):
                    mask |= _reads_mask(k)
        else:
            path = owner
            if attr == "__names__":
                mask |= module_mask(path) | run_module_mask(path)
                continue
            if sub == "__defaults__":
                span = _function_body(path, "", attr)
                if span:
                    mask |= run_range_mask(path, *span) | range_mask(path, *span)
                else:
                    mask |= module_mask(path)
                continue
            refs = symbol_tests(attr)
            mask |= refs if refs else 0
    return mask


def run_module_mask(path):
    found = 0
    for numbers in _run_file(path).values():
        found |= to_mask(numbers)
    return found


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


def select_hunks(path, meta, diff_mode=False):
    selected = 0
    source = source_lines(path)

    def text(line):
        return source[line - 1] if 0 <= line - 1 < len(source) else ""

    for hunk in meta["hunks"]:
        added = hunk["added"]
        chosen = 0
        intentional = False
        undecided = not hunk["changed_old"]
        if hunk["changed_old"]:
            control = any(COND_RE.search(line) for line in added)
            for line in hunk["changed_old"]:
                current = text(line)
                if diff_mode:
                    func = enclosing_func(path, line)
                    if not func:
                        continue  # module/class level: decided by the import-state diff
                    if line <= func[2]:
                        # def / decorator header: the code (or defaults) of this function changed
                        chosen |= run_range_mask(path, func[2], func[1]) | range_mask(path, func[2], func[1])
                        continue
                if DEF_RE.match(current) or IMPORT_RE.match(current):
                    chosen |= ALL
                    continue
                func = enclosing_func(path, line)
                if func:
                    undecided = True
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
                if ATTR_MODE:
                    precise = attr_selection(path, line, meta["hunks"])
                    if precise is not None:
                        chosen |= precise
                        continue
                undecided = True
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
                if diff_mode and not cosmetic:
                    func = enclosing_func(path, line)
                    if not func or enclosing_func(path, insert_line) != func:
                        if any(DEF_RE.match(item) for item in added) or not func:
                            undecided = False
                            continue  # new module/class-level code: decided by the diff
                    elif line < func[2]:
                        chosen |= run_range_mask(path, func[2], func[1]) | range_mask(path, func[2], func[1])
                        continue
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
        if diff_mode and not undecided:
            pass
        elif not chosen and not intentional and undecided:
            anchor = hunk["changed_old"][0] if hunk["changed_old"] else max(1, (hunk["insert_at"][0] if hunk["insert_at"] else 1))
            chosen = escalate(path, anchor, chosen)
        selected |= chosen
    return selected


DIFF_MODE = os.environ.get("DIFF_MODE", "1") == "1"


def _needs_diff(path, meta):
    """True if the patch touches this sqlglot file outside function bodies (module or class
    level, or a def/decorator header)."""
    for hunk in meta["hunks"]:
        lines = list(hunk["changed_old"]) or [max(1, at - 1) for at in hunk["insert_at"]]
        for line in lines:
            func = enclosing_func(path, line)
            if not func or line <= func[2]:
                return True
        if not hunk["changed_old"] and any(DEF_RE.match(item) for item in hunk["added"]):
            return True
    return False


def _patched_sqlglot(files):
    patched = {}
    for path, meta in files.items():
        if meta["new"] or meta["deleted"] or meta["rename"] or meta["binary"]:
            continue
        if path.startswith("sqlglot/") and path.endswith(".py") and read_source(path) is not None:
            patched[path] = patched_text(read_source(path), meta["hunks"])
    return patched


LAST_DIFF = {}


def select(files):
    LAST_DIFF.clear()
    selected = 0
    diff_files = set()
    if DIFF_MODE:
        wanted = [
            path for path, meta in files.items()
            if path.startswith("sqlglot/") and path.endswith(".py") and path != "sqlglot/__init__.py"
            and not (meta["new"] or meta["deleted"] or meta["rename"] or meta["binary"])
            and syntax_ok(path, meta["hunks"]) and get_blocks(path) is not None
            and not (AST_EQ_MODE and ast_equivalent(path, meta["hunks"]))
            and not (EQ_MODE and identity_swap_equivalent(path, meta["hunks"]))
            and _needs_diff(path, meta)
        ]
        if wanted and time.monotonic() - START < BUDGET_SECONDS - 20:
            import verify
            info = verify.state_diff(str(BASE), str(MAP / "state.json"), _patched_sqlglot(files))
            LAST_DIFF["info"] = info
            if info is not None:
                diff_files = set(wanted)
                selected |= diff_selection(info)
                print(f"state diff: {len(info['changed'])} keys", info['changed'][:6] if DEBUG_HEAVY else "", file=sys.stderr)
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
        if EQ_MODE and identity_swap_equivalent(path, meta["hunks"]):
            continue
        if CMP_MODE:
            swapped = swap_selection(path, meta["hunks"])
            if swapped is not None:
                selected |= swapped
                continue
        if get_blocks(path) is None:
            selected |= ALL
            continue
        selected |= select_hunks(path, meta, diff_mode=path in diff_files)
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
        mask = select(files)
    except Exception as error:
        print(f"{patch.name}: {error!r}; using conservative answer", file=sys.stderr)
        return conservative(files)
    if HEAVY_MODE:
        try:
            mask = refine_heavy(files, mask)
        except Exception as error:
            print(f"{patch.name}: heavy check failed: {error!r}", file=sys.stderr)
    if CHEAP_MODE and mask:
        try:
            mask = verify_cheap(files, mask)
        except Exception as error:
            print(f"{patch.name}: cheap check failed: {error!r}", file=sys.stderr)
    return mask


CHEAP_MODE = os.environ.get("CHEAP_MODE", "1") == "1"
CHEAP_MAX_MS = int(os.environ.get("CHEAP_MAX_MS", "1500"))
CHEAP_MAX_TESTS = 40
# Files whose tests need DuckDB/pandas (not in the image), and tests whose outcome depends on
# memory or process state rather than on the code: never decided by running them here.
CHEAP_SKIP_FILES = {"tests/test_executor.py", "tests/test_optimizer.py"}
CHEAP_SKIP_WORDS = ("memory", "leak", "retain", "lazy_load", "sqlglotc", "integration")


def verify_cheap(files, mask):
    """Run a small, cheap selection on the patched code and keep only the tests that fail."""
    for path, meta in files.items():
        if meta["new"] or meta["deleted"] or meta["rename"] or meta["binary"]:
            return mask
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            return mask
    numbers = mask_to_numbers(mask)
    if len(numbers) > CHEAP_MAX_TESTS:
        return mask
    total = sum(DUR_MS.get(n, 0) for n in numbers)
    if total > CHEAP_MAX_MS:
        return mask
    ids = []
    for n in numbers:
        test_id = NUM_ID.get(n)
        if test_id is None or test_id.split("::")[0] in CHEAP_SKIP_FILES:
            return mask
        if any(word in test_id.lower() for word in CHEAP_SKIP_WORDS):
            return mask
        ids.append(test_id)
    if time.monotonic() - START > BUDGET_SECONDS - 15:
        return mask
    patched = _patched_sqlglot(files)
    for path, text in patched.items():
        try:
            compile(text, path, "exec", ast.PyCF_ONLY_AST)
        except Exception:
            return mask
    import verify
    failing = verify.run_tests(str(BASE), patched, ids, seconds=10 + 5 * total / 1000)
    if failing is None:
        return mask
    kept = 0
    for test_id in failing:
        number = ID_NUM.get(test_id)
        if number is not None and (mask >> number) & 1:
            kept |= 1 << number
    print(f"cheap check: ran {len(ids)} tests ({total} ms), {len(failing)} failing", file=sys.stderr)
    return kept


HEAVY_MODE = os.environ.get("HEAVY_MODE", "1") == "1"
DEBUG_HEAVY = os.environ.get("DEBUG_HEAVY") == "1"


def touched_lines(path, hunks):
    """Old line numbers a patch touches, widened to whole statements: the statement for a
    line in a function body, the whole module- or class-level statement otherwise."""
    lines = set()
    raw = set()
    for hunk in hunks:
        raw.update(hunk["changed_old"])
        for at in hunk["insert_at"]:
            raw.update((max(1, at - 1), at))
    tree = base_tree(path)
    for line in raw:
        lines.add(line)
        func = enclosing_func(path, line)
        if func and func[3] == "<lambda>":
            lines.update(range(func[0], func[1] + 1))  # just the lambda, not its statement
            continue
        if func and line > func[2]:
            span = stmt_span(path, line)
            if span:
                lines.update(range(span[0], span[1] + 1))
            continue
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.stmt) and not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.lineno <= line <= node.end_lineno and not enclosing_func(path, node.lineno):
                        lines.update(range(node.lineno, node.end_lineno + 1))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    head_end = node.body[0].lineno - 1 if node.body else node.end_lineno
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    if start <= line <= max(head_end, node.lineno):
                        lines.update(range(start, max(head_end, node.lineno) + 1))
    return lines


_TEST_READS = {}


def test_read_prefixes(number):
    """{"owner|attr"} of every class attribute test `number` read (traced)."""
    if number not in _TEST_READS:
        data = _attrs()
        found = set()
        if data:
            hit_sets = {i for i, members in enumerate(data["sets"]) if number in members}
            for key, idx in data["reads"].items():
                if idx in hit_sets:
                    parts = key.split("|")
                    found.add(parts[0] + "|" + parts[1])
        _TEST_READS[number] = found
    return _TEST_READS[number]


def diff_relevance(info, number):
    """Files owning changed import-state keys that test `number` read, and files owning
    changed keys whose readers are unknown (module globals, method sets, uninstrumented
    classes)."""
    reads = test_read_prefixes(number)
    owners = _traced_owners()
    read_files, unknown_files = set(), set()
    for key in info["changed"]:
        owner, rest = key.split("|", 1)
        attr = rest.split("|", 1)[0]
        sub = rest.split("|", 1)[1] if "|" in rest else None
        if sub == "__defaults__":
            continue  # handled as a change to the function body
        path = owner.split(":", 1)[0]
        if ":" not in owner or attr == "__methods__" or ".<" in owner or owner not in owners:
            unknown_files.add(path)
        elif (owner + "|" + attr) in reads:
            read_files.add(path)
    return read_files, unknown_files


def refine_heavy(files, mask):
    """Drop heavy tests that decision-time checks on the patched code prove cannot fail."""
    import verify
    heavy = {}
    for test_id in (verify.TPCDS, verify.TPCH, verify.OPT_TPCH, verify.MAX_NODES, verify.GEN_MEMORY, verify.ANNOTATE_FUNCS):
        number = ID_NUM.get(test_id)
        if number is not None and (mask >> number) & 1:
            heavy[test_id] = number
    if not heavy:
        return mask
    if time.monotonic() - START > BUDGET_SECONDS - 15:
        return mask
    patched, runtime, import_time = {}, {}, {}
    for path, meta in files.items():
        if meta["new"] or meta["deleted"] or meta["rename"] or meta["binary"]:
            return mask
        if path.startswith("tests/fixtures/"):
            # a heavy test that opens this fixture stays; the others are unaffected by it
            readers = fixture_reader_mask(os.path.splitext(os.path.basename(path))[0])
            if readers is None:
                return mask
            for test_id in [t for t, n in heavy.items() if (readers >> n) & 1]:
                del heavy[test_id]
            if not heavy:
                return mask
            continue
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            return mask
        text = read_source(path)
        if text is None:
            return mask
        new_text = patched_text(text, meta["hunks"])
        try:
            compile(new_text, path, "exec", ast.PyCF_ONLY_AST)
        except Exception:
            return mask
        patched[path] = new_text
        for line in touched_lines(path, meta["hunks"]):
            func = enclosing_func(path, line)
            if func and (line > func[2] or func[3] == "<lambda>"):
                runtime.setdefault(path, set()).add(line)
            else:
                import_time.setdefault(path, set()).add(line)
                if func:  # def / decorator header: the body's behaviour may change
                    runtime.setdefault(path, set()).update(range(func[2], func[1] + 1))
    data = verify.tpc_map(str(MAP))
    info = LAST_DIFF.get("info")
    checks = {}
    for test_id, number in heavy.items():
        if test_id == verify.MAX_NODES:
            checks[test_id] = None
            continue
        if test_id in (verify.GEN_MEMORY, verify.ANNOTATE_FUNCS):
            # per-dialect subtests: re-run them for the dialects the change can reach
            if import_time and info is None:
                continue
            owners = sorted({k.split("|", 1)[0] for k in (info["changed"] if info else []) if ":" in k.split("|", 1)[0]})
            module_keys = [k for k in (info["changed"] if info else []) if ":" not in k.split("|", 1)[0]]
            if module_keys:
                continue
            dialects = verify.affected_dialects(str(MAP / "state.json"), owners, sorted(runtime))
            if dialects is None or len(dialects) > verify.MAX_SUBSET_DIALECTS:
                continue
            checks[test_id] = dialects
            continue
        if not data:
            continue
        # body lines that ran during import or TestExecutor.setUpClass can break every test
        if any(lines & data["setup"].get(path, set()) for path, lines in runtime.items()):
            if DEBUG_HEAVY:
                print("keep(setup)", test_id.split("::")[-1], {p: sorted(l & data["setup"].get(p, set()))[:5] for p, l in runtime.items()}, file=sys.stderr)
            continue
        import_relevant = False
        if import_time and test_id == verify.TPCDS:
            # TPCH checks re-derive everything they use on the patched code; TPC-DS's run phase
            # is not re-run, so import-time changes it can see there must keep it.
            if info is None:
                if any(path in data["exec"] for path in import_time):
                    continue
                import_relevant = True
            else:
                read_files, unknown_files = diff_relevance(info, number)
                if any(f in data["exec"] for f in read_files | unknown_files):
                    if DEBUG_HEAVY:
                        print("keep(diff)", read_files, unknown_files, info["changed"][:4], file=sys.stderr)
                    continue
                import_relevant = bool(read_files or unknown_files)
        if test_id == verify.TPCDS:
            if any(lines & data["exec"].get(path, set()) for path, lines in runtime.items()):
                if DEBUG_HEAVY:
                    print("keep(exec)", {p: sorted(l & data["exec"].get(p, set()))[:5] for p, l in runtime.items()}, file=sys.stderr)
                continue
            if import_relevant:
                checks[test_id] = [q["i"] for q in data["tpcds"]]
                continue
            action, queries = verify.plan_tpcds(data, runtime)
            if action == "drop":
                mask &= ~(1 << number)
            elif action == "check":
                checks[test_id] = queries
        elif test_id == verify.TPCH:
            if any(lines & data["exec"].get(path, set()) for path, lines in runtime.items()):
                continue
            checks[test_id] = None
        else:
            checks[test_id] = None
    if checks:
        t0 = os.times()
        safe = verify.run_checks(str(BASE), str(MAP), patched, checks)
        t1 = os.times()
        spent = (t1.children_user + t1.children_system) - (t0.children_user + t0.children_system)
        for test_id in safe:
            mask &= ~(1 << heavy[test_id])
        print(f"heavy checks {sorted(t.split('::')[-1] for t in checks)} safe {sorted(t.split('::')[-1] for t in safe)} cpu {spent:.2f}", file=sys.stderr)
    return mask


def main():
    gc.disable()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
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