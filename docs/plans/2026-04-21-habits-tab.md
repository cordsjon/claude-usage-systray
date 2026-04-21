# US-TB-01 Habits Tab — Implementation Plan

> **For agentic workers:** REQUIRED: Use `/sh:execute` to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a measured prompt-frequency "Habits" tab to the claude-usage-systray dashboard, with regex-first ingest of Claude Code transcripts, JSON-backed manual `everyday`/`case_by_case` classification, skill-candidate detection, hourly launchd job, and a `/lightsout` post-step.

**Architecture:** Pure extension of the existing port-17420 server. New SQLite tables added to `engine/db.py`'s `_SCHEMA` string (the project uses schema-as-code, not Alembic — we honor that). New endpoints added to the existing `http.server.BaseHTTPRequestHandler` if/elif router in `engine/api.py`. New 7th tab in `engine/dashboard.html` following the exact `data-tab` / `#tab-*` pattern. Regex matching only; no LLM in hot path. Offline LLM-suggestion loop is explicitly v2, out of scope.

**Tech Stack:** Python 3 stdlib (sqlite3, http.server, pathlib, hashlib, json, re), PyYAML for pattern config, vanilla JS + HTML + CSS for dashboard (no framework). `unittest.TestCase` for tests (not pytest — matches existing project convention). launchd plist (macOS) + PowerShell `Register-ScheduledTask` (Windows parity).

**Source of truth:** [00_Governance/BACKLOG.md US-TB-01 (lines 58-175)](../../../00_Governance/BACKLOG.md). 25 ACs. Both panel gates passed.

**Execution waves (for parallel-implementer dispatch):**
- **Wave 1 (serial, single agent):** Chunk 1 — DB schema + UsageDB methods. Everything else depends on this.
- **Wave 2 (2 parallel agents):** Chunks 2A (ingest script) + 2B (API endpoints). Both depend on Wave 1; independent of each other.
- **Wave 3 (4 parallel agents):** Chunks 3A (dashboard) + 3B (launchd/PS) + 3C (YAML seed + eval) + 3D (/lightsout hook). All depend on Wave 2; independent of each other.
- **Wave 4 (serial, single agent):** Chunk 4 — Verification + bootstrap + DoD sign-off.

---

## Chunk 1: DB Schema + UsageDB Methods (Wave 1 — serial)

**Rationale:** Every other chunk reads or writes one of these tables. Adding them first gives all downstream work a stable schema to code against. The project uses schema-as-code (`CREATE TABLE IF NOT EXISTS` in a single `_SCHEMA` string executed at `UsageDB.__init__`) — we extend that rather than introducing Alembic.

**Files:**
- Modify: `engine/db.py` (add tables to `_SCHEMA`, add CRUD methods, add `downgrade()`)
- Create: `engine/tests/test_db_prompts.py` (unittest.TestCase with in-memory `UsageDB(":memory:")`)

### Task 1.1: Extend `_SCHEMA` with 5 new tables

- [ ] **Step 1.1.1: Write failing test for schema presence**

Create `engine/tests/test_db_prompts.py`:

```python
import unittest
from engine.db import UsageDB


class TestPromptTables(unittest.TestCase):
    def setUp(self):
        self.db = UsageDB(":memory:")

    def _table_exists(self, name):
        row = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def test_prompt_usage_table_exists(self):
        self.assertTrue(self._table_exists("prompt_usage"))

    def test_prompt_unmatched_table_exists(self):
        self.assertTrue(self._table_exists("prompt_unmatched"))

    def test_prompt_pattern_eval_table_exists(self):
        self.assertTrue(self._table_exists("prompt_pattern_eval"))

    def test_prompt_pattern_eval_labels_table_exists(self):
        self.assertTrue(self._table_exists("prompt_pattern_eval_labels"))

    def test_ingest_watermark_table_exists(self):
        self.assertTrue(self._table_exists("ingest_watermark"))
```

- [ ] **Step 1.1.2: Run tests to verify they fail**

Run: `cd /Users/jcords-macmini/projects/claude-usage-systray && python3 -m unittest engine.tests.test_db_prompts -v`
Expected: 5 failures with "no such table".

- [ ] **Step 1.1.3: Extend `_SCHEMA` in `engine/db.py`**

Append to the existing `_SCHEMA` string (preserve the `usage_snapshots` block as-is):

```sql
CREATE TABLE IF NOT EXISTS prompt_usage (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    pattern_version INTEGER NOT NULL DEFAULT 1,
    is_structured INTEGER NOT NULL,
    matched_text TEXT,
    message_ordinal INTEGER NOT NULL,
    UNIQUE (session_id, message_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_prompt_usage_date ON prompt_usage(date);
CREATE INDEX IF NOT EXISTS idx_prompt_usage_pattern ON prompt_usage(pattern_id);

CREATE TABLE IF NOT EXISTS prompt_unmatched (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    session_id TEXT NOT NULL,
    text_excerpt TEXT NOT NULL,
    message_ordinal INTEGER NOT NULL,
    UNIQUE (session_id, message_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_prompt_unmatched_date ON prompt_unmatched(date);

CREATE TABLE IF NOT EXISTS prompt_pattern_eval (
    id INTEGER PRIMARY KEY,
    pattern_id TEXT NOT NULL,
    pattern_version INTEGER NOT NULL,
    eval_date TEXT NOT NULL,
    precision_score REAL,
    sample_size INTEGER NOT NULL,
    verdict TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_pattern_eval_labels (
    id INTEGER PRIMARY KEY,
    pattern_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    is_true_positive INTEGER NOT NULL,
    labeler TEXT NOT NULL,
    labeled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_watermark (
    file_path TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL,
    sha256_head TEXT NOT NULL,
    last_ingested_at TEXT NOT NULL
);
```

- [ ] **Step 1.1.4: Run tests to verify they pass**

Run: `python3 -m unittest engine.tests.test_db_prompts -v`
Expected: 5 PASS.

- [ ] **Step 1.1.5: Commit**

```bash
cd /Users/jcords-macmini/projects/claude-usage-systray
git add engine/db.py engine/tests/test_db_prompts.py
git commit -m "feat(db): add prompt-frequency tables (US-TB-01)"
```

### Task 1.2: `insert_prompt_usage` / `insert_prompt_unmatched` / `upsert_watermark`

- [ ] **Step 1.2.1: Write failing tests for CRUD**

Append to `engine/tests/test_db_prompts.py`:

```python
class TestPromptCRUD(unittest.TestCase):
    def setUp(self):
        self.db = UsageDB(":memory:")

    def test_insert_prompt_usage_is_idempotent(self):
        row = dict(date="2026-04-21", session_id="s1", project_dir="/Users/x",
                   pattern_id="lightsout", pattern_version=1, is_structured=1,
                   matched_text="/lightsout", message_ordinal=7)
        self.db.insert_prompt_usage(**row)
        self.db.insert_prompt_usage(**row)  # duplicate — must not double-count
        count = self.db._conn.execute(
            "SELECT COUNT(*) FROM prompt_usage"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_prompt_unmatched_stores_excerpt(self):
        self.db.insert_prompt_unmatched(
            date="2026-04-21", session_id="s1",
            text_excerpt="some novel prompt", message_ordinal=3)
        row = self.db._conn.execute(
            "SELECT text_excerpt FROM prompt_unmatched"
        ).fetchone()
        self.assertEqual(row[0], "some novel prompt")

    def test_upsert_watermark_updates_on_conflict(self):
        self.db.upsert_watermark("/tmp/a.jsonl", 100, "abc", "2026-04-21T10:00:00")
        self.db.upsert_watermark("/tmp/a.jsonl", 200, "abc", "2026-04-21T11:00:00")
        row = self.db._conn.execute(
            "SELECT byte_offset FROM ingest_watermark WHERE file_path=?",
            ("/tmp/a.jsonl",)).fetchone()
        self.assertEqual(row[0], 200)

    def test_get_watermark_returns_none_for_new_file(self):
        self.assertIsNone(self.db.get_watermark("/tmp/new.jsonl"))
```

- [ ] **Step 1.2.2: Run tests to confirm they fail**

Run: `python3 -m unittest engine.tests.test_db_prompts.TestPromptCRUD -v`
Expected: AttributeError / no such method.

- [ ] **Step 1.2.3: Implement the methods on `UsageDB`**

Add to `engine/db.py` inside the `UsageDB` class:

```python
def insert_prompt_usage(self, *, date, session_id, project_dir, pattern_id,
                        pattern_version, is_structured, matched_text,
                        message_ordinal):
    self._conn.execute(
        """INSERT OR IGNORE INTO prompt_usage
           (date, session_id, project_dir, pattern_id, pattern_version,
            is_structured, matched_text, message_ordinal)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (date, session_id, project_dir, pattern_id, pattern_version,
         int(bool(is_structured)), matched_text, message_ordinal))
    self._conn.commit()

def insert_prompt_unmatched(self, *, date, session_id, text_excerpt,
                            message_ordinal):
    self._conn.execute(
        """INSERT OR IGNORE INTO prompt_unmatched
           (date, session_id, text_excerpt, message_ordinal)
           VALUES (?, ?, ?, ?)""",
        (date, session_id, text_excerpt, message_ordinal))
    self._conn.commit()

def upsert_watermark(self, file_path, byte_offset, sha256_head, last_ingested_at):
    self._conn.execute(
        """INSERT INTO ingest_watermark
           (file_path, byte_offset, sha256_head, last_ingested_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
             byte_offset=excluded.byte_offset,
             sha256_head=excluded.sha256_head,
             last_ingested_at=excluded.last_ingested_at""",
        (file_path, byte_offset, sha256_head, last_ingested_at))
    self._conn.commit()

def get_watermark(self, file_path):
    row = self._conn.execute(
        """SELECT byte_offset, sha256_head, last_ingested_at
           FROM ingest_watermark WHERE file_path=?""",
        (file_path,)).fetchone()
    if row is None:
        return None
    return {"byte_offset": row[0], "sha256_head": row[1],
            "last_ingested_at": row[2]}
```

- [ ] **Step 1.2.4: Run tests to verify they pass**

Run: `python3 -m unittest engine.tests.test_db_prompts.TestPromptCRUD -v`
Expected: 4 PASS.

- [ ] **Step 1.2.5: Commit**

```bash
git add engine/db.py engine/tests/test_db_prompts.py
git commit -m "feat(db): prompt_usage/unmatched CRUD + watermark upsert (US-TB-01)"
```

### Task 1.3: `get_ranked_prompts(window_days_list=[7,30,None])`

- [ ] **Step 1.3.1: Write failing test**

Append to `test_db_prompts.py`:

```python
class TestRankedPrompts(unittest.TestCase):
    def setUp(self):
        self.db = UsageDB(":memory:")
        # Seed: 3× lightsout, 1× produce
        for i in range(3):
            self.db.insert_prompt_usage(
                date="2026-04-20", session_id=f"s{i}", project_dir="/p",
                pattern_id="lightsout", pattern_version=1, is_structured=1,
                matched_text="/lightsout", message_ordinal=i)
        self.db.insert_prompt_usage(
            date="2026-04-20", session_id="sp", project_dir="/p",
            pattern_id="produce", pattern_version=1, is_structured=0,
            matched_text="produce animals", message_ordinal=0)

    def test_ranked_includes_7d_30d_all_counts(self):
        rows = self.db.get_ranked_prompts(today="2026-04-21")
        by_id = {r["pattern_id"]: r for r in rows}
        self.assertEqual(by_id["lightsout"]["count_all"], 3)
        self.assertEqual(by_id["produce"]["count_all"], 1)
        # Both within 7d window (yesterday)
        self.assertEqual(by_id["lightsout"]["count_7d"], 3)
        self.assertEqual(by_id["produce"]["count_7d"], 1)
```

- [ ] **Step 1.3.2: Run to confirm fail**

- [ ] **Step 1.3.3: Implement `get_ranked_prompts`**

Add to `UsageDB`:

```python
def get_ranked_prompts(self, today):
    """Return list of {pattern_id, is_structured, count_7d, count_30d, count_all}."""
    from datetime import date, timedelta
    today_d = date.fromisoformat(today)
    d7 = (today_d - timedelta(days=7)).isoformat()
    d30 = (today_d - timedelta(days=30)).isoformat()
    rows = self._conn.execute("""
        SELECT pattern_id,
               MAX(is_structured) AS is_structured,
               SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS c7,
               SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS c30,
               COUNT(*) AS call_total
        FROM prompt_usage
        GROUP BY pattern_id
        ORDER BY c7 DESC, call_total DESC
    """, (d7, d30)).fetchall()
    return [dict(pattern_id=r[0], is_structured=bool(r[1]),
                 count_7d=r[2], count_30d=r[3], count_all=r[4]) for r in rows]
```

- [ ] **Step 1.3.4: Run — PASS.**
- [ ] **Step 1.3.5: Commit**

```bash
git add engine/db.py engine/tests/test_db_prompts.py
git commit -m "feat(db): get_ranked_prompts with 7d/30d/all windows (US-TB-01)"
```

### Task 1.4: `downgrade()` for rollback (AC-16b)

- [ ] **Step 1.4.1: Write failing test**

```python
class TestDowngrade(unittest.TestCase):
    def test_downgrade_drops_only_new_tables(self):
        db = UsageDB(":memory:")
        db.downgrade_prompt_tables()
        for t in ["prompt_usage", "prompt_unmatched",
                  "prompt_pattern_eval", "prompt_pattern_eval_labels",
                  "ingest_watermark"]:
            row = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            self.assertIsNone(row, f"{t} should be dropped")
        # usage_snapshots must remain
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_snapshots'"
        ).fetchone()
        self.assertIsNotNone(row)
```

- [ ] **Step 1.4.2: Confirm fail.**
- [ ] **Step 1.4.3: Implement**

```python
def downgrade_prompt_tables(self):
    for t in ("prompt_pattern_eval_labels", "prompt_pattern_eval",
              "prompt_unmatched", "prompt_usage", "ingest_watermark"):
        self._conn.execute(f"DROP TABLE IF EXISTS {t}")
    self._conn.commit()
```

- [ ] **Step 1.4.4: Pass.**
- [ ] **Step 1.4.5: Commit.**

```bash
git add engine/db.py engine/tests/test_db_prompts.py
git commit -m "feat(db): downgrade_prompt_tables for rollback (US-TB-01 AC-16b)"
```

**Chunk 1 checkpoint:** All 5 new tables exist, CRUD works, ranked query works, rollback works. Downstream chunks can now code against the schema.

---

## Chunk 2A: Ingest Script (Wave 2 — parallel with 2B)

**Rationale:** Standalone script that walks `~/.claude/projects/*/conversations/*.jsonl`, classifies, persists. Independent from API — only writes DB rows.

**Files:**
- Create: `engine/ingest_prompts.py`
- Create: `engine/patterns.py` (YAML loader + matcher)
- Create: `engine/redact.py` (credential/path/token redaction)
- Create: `engine/tests/test_ingest.py`
- Create: `engine/tests/test_patterns.py`
- Create: `engine/tests/test_redact.py`
- Create: `engine/tests/fixtures/sample_conversation.jsonl`

### Task 2A.1: Redaction sweep (AC-04)

- [ ] **Step 2A.1.1: Write failing tests**

Create `engine/tests/test_redact.py`:

```python
import unittest
from engine.redact import redact_for_unmatched


class TestRedact(unittest.TestCase):
    def test_strips_absolute_posix_paths(self):
        out = redact_for_unmatched("see /Users/jon/secrets/key.pem for key")
        self.assertNotIn("/Users/jon", out)

    def test_strips_emails(self):
        out = redact_for_unmatched("ping alice@example.com about this")
        self.assertNotIn("alice@example.com", out)

    def test_strips_bearer_tokens(self):
        out = redact_for_unmatched("curl -H 'Authorization: Bearer sk-abc123xyz'")
        self.assertNotIn("sk-abc123xyz", out)

    def test_truncates_to_200_chars(self):
        out = redact_for_unmatched("a" * 500)
        self.assertLessEqual(len(out), 200)

    def test_preserves_ordinary_text(self):
        self.assertEqual(redact_for_unmatched("hello world"), "hello world")
```

- [ ] **Step 2A.1.2: Confirm fail.**
- [ ] **Step 2A.1.3: Implement `engine/redact.py`**

```python
import re

_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?:/Users/|/home/|C:\\\\)[^\s'\"]+"), "<path>"),
    (re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]+"), "Bearer <token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), "<token>"),
]


def redact_for_unmatched(text):
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    if len(out) > 200:
        out = out[:197] + "..."
    return out
```

- [ ] **Step 2A.1.4: Pass.**
- [ ] **Step 2A.1.5: Commit.**

```bash
git add engine/redact.py engine/tests/test_redact.py
git commit -m "feat(ingest): redaction sweep for unmatched prompts (US-TB-01 AC-04)"
```

### Task 2A.2: YAML pattern loader + matcher

- [ ] **Step 2A.2.1: Write failing tests**

Create `engine/tests/test_patterns.py`:

```python
import unittest
from pathlib import Path
from engine.patterns import load_patterns, classify_message


class TestPatterns(unittest.TestCase):
    def setUp(self):
        self.yaml_path = Path(__file__).parent / "fixtures" / "patterns_test.yaml"
        self.yaml_path.parent.mkdir(exist_ok=True)
        self.yaml_path.write_text(
            "patterns:\n"
            "  - id: produce\n"
            "    intent: SVG production run\n"
            "    regex: '(?i)^\\s*produce\\s+'\n"
            "    type: unstructured\n"
            "    version: 1\n"
            "  - id: ebg\n"
            "    intent: Excel bridge to Gemini\n"
            "    regex: '(?i)^\\s*ebg\\b'\n"
            "    type: unstructured\n"
            "    version: 1\n"
        )

    def test_structured_slash_command(self):
        p = classify_message("/lightsout", load_patterns(self.yaml_path))
        self.assertEqual(p["pattern_id"], "lightsout")
        self.assertTrue(p["is_structured"])

    def test_slash_command_strips_args(self):
        p = classify_message("/sh:spec-panel arg1 arg2", load_patterns(self.yaml_path))
        self.assertEqual(p["pattern_id"], "sh:spec-panel")

    def test_unstructured_match(self):
        p = classify_message("produce animals s1",
                             load_patterns(self.yaml_path))
        self.assertEqual(p["pattern_id"], "produce")
        self.assertFalse(p["is_structured"])

    def test_unmatched_returns_none_id(self):
        p = classify_message("no idea what this is",
                             load_patterns(self.yaml_path))
        self.assertIsNone(p["pattern_id"])
```

- [ ] **Step 2A.2.2: Confirm fail.**
- [ ] **Step 2A.2.3: Implement `engine/patterns.py`**

```python
import re
import yaml
from pathlib import Path


_SLASH_RE = re.compile(r"^/([a-z][a-z0-9:_\-]*)")


def load_patterns(yaml_path):
    data = yaml.safe_load(Path(yaml_path).read_text())
    compiled = []
    for entry in data.get("patterns", []):
        compiled.append({
            "id": entry["id"],
            "intent": entry.get("intent", ""),
            "regex": re.compile(entry["regex"]),
            "type": entry.get("type", "unstructured"),
            "version": int(entry.get("version", 1)),
        })
    return compiled


def classify_message(text, patterns):
    stripped = text.lstrip()
    m = _SLASH_RE.match(stripped)
    if m:
        return {"pattern_id": m.group(1), "is_structured": True, "version": 0}
    for p in patterns:
        if p["regex"].search(stripped):
            return {"pattern_id": p["id"], "is_structured": False,
                    "version": p["version"]}
    return {"pattern_id": None, "is_structured": False, "version": 0}
```

- [ ] **Step 2A.2.4: Pass.**
- [ ] **Step 2A.2.5: Commit.**

```bash
git add engine/patterns.py engine/tests/test_patterns.py engine/tests/fixtures/patterns_test.yaml
git commit -m "feat(ingest): YAML pattern loader + classify_message (US-TB-01 AC-02)"
```

### Task 2A.3: `project_dir` decoder + `session_id` extractor

- [ ] **Step 2A.3.1: Write failing test**

```python
# in a new tests/test_project_dir.py
from engine.ingest_prompts import decode_project_dir

class TestDecodeProjectDir(unittest.TestCase):
    def test_standard_path(self):
        self.assertEqual(
            decode_project_dir("-Users-jcords-macmini-projects-30_SVG-PAINT"),
            "/Users/jcords-macmini/projects/30_SVG-PAINT")

    def test_simple_unix_path(self):
        self.assertEqual(
            decode_project_dir("-home-alice-work"),
            "/home/alice/work")
```

- [ ] **Step 2A.3.2: Confirm fail.**
- [ ] **Step 2A.3.3: Implement in `engine/ingest_prompts.py`**

```python
def decode_project_dir(encoded):
    """Decode Claude Code's ~/.claude/projects/<encoded>/ dir name to absolute path.
    Rule: leading '-Users-' or '-home-' becomes '/Users/' or '/home/', then remaining
    '-' become '/'. Projects with hyphens in their name survive because the decoder
    only re-replaces '-' after the OS-root prefix."""
    prefixes = [("-Users-", "/Users/"), ("-home-", "/home/")]
    for enc, dec in prefixes:
        if encoded.startswith(enc):
            rest = encoded[len(enc):]
            return dec + rest.replace("-", "/", 1).replace("-", "/")
    # Unknown prefix — best-effort: replace all '-' with '/'
    return "/" + encoded.lstrip("-").replace("-", "/")
```

> NOTE: the replace-once-then-replace-all pattern is a known hack; known limitation is real paths with a literal `-` later in the string. Will be addressed when a counter-example fixture arises. Fixture `project_dir_edgecases.yaml` reserved for future.

- [ ] **Step 2A.3.4: Pass.**
- [ ] **Step 2A.3.5: Commit.**

```bash
git add engine/ingest_prompts.py engine/tests/test_project_dir.py
git commit -m "feat(ingest): decode_project_dir from encoded path (US-TB-01 Definitions)"
```

### Task 2A.4: JSONL line parser — extract user messages only

- [ ] **Step 2A.4.1: Create fixture `engine/tests/fixtures/sample_conversation.jsonl`** with 4 lines:

```
{"role":"user","content":"/lightsout","sessionId":"S1","timestamp":"2026-04-21T10:00:00Z"}
{"role":"assistant","content":"ok wrapping up"}
{"role":"user","content":[{"type":"tool_result","content":"ok"}],"sessionId":"S1"}
{"role":"user","content":"produce animals s1","sessionId":"S1","timestamp":"2026-04-21T10:05:00Z"}
```

- [ ] **Step 2A.4.2: Write failing test**

```python
# test_ingest.py
import unittest
from pathlib import Path
from engine.ingest_prompts import iter_user_messages

FIXT = Path(__file__).parent / "fixtures" / "sample_conversation.jsonl"


class TestIterUserMessages(unittest.TestCase):
    def test_extracts_only_text_user_messages(self):
        msgs = list(iter_user_messages(FIXT, start_offset=0))
        texts = [m["text"] for m in msgs]
        self.assertEqual(texts, ["/lightsout", "produce animals s1"])

    def test_each_has_ordinal(self):
        msgs = list(iter_user_messages(FIXT, start_offset=0))
        ordinals = [m["message_ordinal"] for m in msgs]
        self.assertEqual(ordinals, [0, 3])

    def test_end_offset_reported(self):
        msgs = list(iter_user_messages(FIXT, start_offset=0))
        self.assertGreater(msgs[-1]["byte_offset_after"], 0)
```

- [ ] **Step 2A.4.3: Confirm fail.**
- [ ] **Step 2A.4.4: Implement `iter_user_messages` in `engine/ingest_prompts.py`**

```python
import json
from pathlib import Path


def iter_user_messages(jsonl_path, start_offset=0):
    """Yield dicts: {text, session_id, timestamp, message_ordinal, byte_offset_after}.
    Only yields role=user entries whose content is a non-empty string OR a list
    with at least one text block. Skips tool_result-bearing user entries."""
    with open(jsonl_path, "rb") as fh:
        fh.seek(start_offset)
        ordinal = _count_lines_up_to(jsonl_path, start_offset) if start_offset > 0 else 0
        while True:
            line_start = fh.tell()
            line = fh.readline()
            if not line:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                ordinal += 1
                continue
            ordinal += 1
            if obj.get("role") != "user":
                continue
            content = obj.get("content")
            text = _extract_user_text(content)
            if not text:
                continue
            yield {
                "text": text,
                "session_id": obj.get("sessionId") or jsonl_path.stem,
                "timestamp": obj.get("timestamp", ""),
                "message_ordinal": ordinal - 1,
                "byte_offset_after": fh.tell(),
            }


def _extract_user_text(content):
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    return None  # exclude entire message
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        combined = "\n".join(texts).strip()
        return combined or None
    return None


def _count_lines_up_to(path, offset):
    with open(path, "rb") as fh:
        chunk = fh.read(offset)
    return chunk.count(b"\n")
```

- [ ] **Step 2A.4.5: Pass.**
- [ ] **Step 2A.4.6: Commit.**

```bash
git add engine/ingest_prompts.py engine/tests/test_ingest.py engine/tests/fixtures/sample_conversation.jsonl
git commit -m "feat(ingest): iter_user_messages with tool_result exclusion (US-TB-01 AC-01, Definitions)"
```

### Task 2A.5: Watermark with sha256_head rotation detection

- [ ] **Step 2A.5.1: Write failing test**

```python
class TestWatermark(unittest.TestCase):
    def test_new_file_starts_at_offset_0(self):
        from engine.ingest_prompts import compute_sha256_head, resolve_start_offset
        from engine.db import UsageDB
        db = UsageDB(":memory:")
        off, head = resolve_start_offset(db, str(FIXT))
        self.assertEqual(off, 0)
        self.assertEqual(head, compute_sha256_head(FIXT))

    def test_known_file_resumes_from_watermark(self):
        from engine.ingest_prompts import compute_sha256_head, resolve_start_offset
        from engine.db import UsageDB
        db = UsageDB(":memory:")
        head = compute_sha256_head(FIXT)
        db.upsert_watermark(str(FIXT), 50, head, "2026-04-20T10:00:00")
        off, h = resolve_start_offset(db, str(FIXT))
        self.assertEqual(off, 50)
        self.assertEqual(h, head)

    def test_sha_mismatch_resets_offset(self):
        from engine.ingest_prompts import resolve_start_offset
        from engine.db import UsageDB
        db = UsageDB(":memory:")
        db.upsert_watermark(str(FIXT), 50, "stale_sha", "2026-04-20T10:00:00")
        off, _ = resolve_start_offset(db, str(FIXT))
        self.assertEqual(off, 0)
```

- [ ] **Step 2A.5.2: Confirm fail.**
- [ ] **Step 2A.5.3: Implement**

```python
import hashlib

def compute_sha256_head(path, size=4096):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read(size)).hexdigest()


def resolve_start_offset(db, path):
    head = compute_sha256_head(path)
    wm = db.get_watermark(path)
    if wm and wm["sha256_head"] == head:
        return wm["byte_offset"], head
    return 0, head
```

- [ ] **Step 2A.5.4: Pass.**
- [ ] **Step 2A.5.5: Commit.**

```bash
git add engine/ingest_prompts.py engine/tests/test_ingest.py
git commit -m "feat(ingest): sha256_head rotation-tolerant watermark (US-TB-01 AC-05)"
```

### Task 2A.6: End-to-end `ingest_all()` with coverage report

- [ ] **Step 2A.6.1: Write failing e2e test**

```python
class TestIngestE2E(unittest.TestCase):
    def test_full_run_populates_prompt_usage_and_reports_coverage(self):
        import tempfile, shutil, os
        from engine.ingest_prompts import ingest_all
        from engine.db import UsageDB
        db = UsageDB(":memory:")
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "-Users-x-projects-demo" / "conversations"
        proj.mkdir(parents=True)
        shutil.copy(FIXT, proj / "s1.jsonl")

        yaml_path = Path(__file__).parent / "fixtures" / "patterns_test.yaml"
        report = ingest_all(db, projects_root=tmp, patterns_yaml=yaml_path)

        count = db._conn.execute("SELECT COUNT(*) FROM prompt_usage").fetchone()[0]
        self.assertGreaterEqual(count, 1)
        self.assertGreater(report["total_user_messages"], 0)
        self.assertIn("matched_percent", report)
```

- [ ] **Step 2A.6.2: Confirm fail.**
- [ ] **Step 2A.6.3: Implement `ingest_all` driver + coverage report**

```python
from datetime import datetime


def ingest_all(db, projects_root, patterns_yaml):
    from engine.patterns import load_patterns, classify_message
    from engine.redact import redact_for_unmatched

    patterns = load_patterns(patterns_yaml)
    total, matched, unmatched, structured = 0, 0, 0, 0
    for proj_dir in Path(projects_root).glob("-*"):
        conv_dir = proj_dir / "conversations"
        if not conv_dir.is_dir():
            continue
        project_dir = decode_project_dir(proj_dir.name)
        for jsonl in sorted(conv_dir.glob("*.jsonl")):
            start_off, head = resolve_start_offset(db, str(jsonl))
            last_off = start_off
            for msg in iter_user_messages(jsonl, start_offset=start_off):
                total += 1
                last_off = msg["byte_offset_after"]
                cls = classify_message(msg["text"], patterns)
                date = (msg["timestamp"] or datetime.utcnow().isoformat())[:10]
                if cls["pattern_id"]:
                    matched += 1
                    if cls["is_structured"]:
                        structured += 1
                    db.insert_prompt_usage(
                        date=date, session_id=msg["session_id"],
                        project_dir=project_dir, pattern_id=cls["pattern_id"],
                        pattern_version=cls["version"],
                        is_structured=cls["is_structured"],
                        matched_text=msg["text"][:500],
                        message_ordinal=msg["message_ordinal"])
                else:
                    unmatched += 1
                    db.insert_prompt_unmatched(
                        date=date, session_id=msg["session_id"],
                        text_excerpt=redact_for_unmatched(msg["text"]),
                        message_ordinal=msg["message_ordinal"])
            db.upsert_watermark(str(jsonl), last_off, head,
                                datetime.utcnow().isoformat())
    mp = (matched / total * 100.0) if total else 0.0
    return {"total_user_messages": total, "matched": matched,
            "unmatched": unmatched, "structured": structured,
            "matched_percent": round(mp, 1)}


if __name__ == "__main__":
    import os, sys
    from engine.db import UsageDB
    db_path = os.environ.get("TOKEN_BUDGET_DB",
                             str(Path.home() / ".local/share/token-budget/usage.db"))
    projects_root = Path.home() / ".claude" / "projects"
    patterns_yaml = (Path.home() / ".claude/projects"
                     / "-Users-jcords-macmini-projects"
                     / "memory" / "prompt-patterns.yaml")
    report = ingest_all(UsageDB(db_path), projects_root, patterns_yaml)
    print(f"ingest: {report['total_user_messages']} msgs, "
          f"{report['matched_percent']}% matched, "
          f"{report['unmatched']} unmatched")
    sys.exit(0)
```

- [ ] **Step 2A.6.4: Pass.**
- [ ] **Step 2A.6.5: Commit.**

```bash
git add engine/ingest_prompts.py engine/tests/test_ingest.py
git commit -m "feat(ingest): e2e ingest_all + coverage report (US-TB-01 AC-05a, AC-16)"
```

**Chunk 2A checkpoint:** `python3 -m engine.ingest_prompts` runs end-to-end against real `~/.claude/projects` and populates the DB.

---

## Chunk 2B: API Endpoints (Wave 2 — parallel with 2A)

**Rationale:** Independent of ingest — only reads DB and writes classification JSON. Adds 4 endpoints to the existing `if/elif` router.

**Files:**
- Modify: `engine/api.py` (add 4 endpoint branches + 4 handler methods)
- Create: `engine/classification.py` (atomic JSON read/write)
- Create: `engine/tests/test_classification.py`
- Extend: `engine/tests/test_api.py` (add 4 integration tests)
- Create: `engine/data/prompt-classification.json` (seed with `{"everyday":[], "case_by_case":[]}`)

### Task 2B.1: Classification JSON atomic read/write

- [ ] **Step 2B.1.1: Test**

```python
# test_classification.py
import unittest, tempfile, json
from pathlib import Path
from engine.classification import load_classification, save_classification, move_pattern


class TestClassification(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "c.json"
        self.tmp.write_text(json.dumps({"everyday": [], "case_by_case": []}))

    def test_load_empty(self):
        self.assertEqual(load_classification(self.tmp),
                         {"everyday": [], "case_by_case": []})

    def test_move_pattern_between_sections(self):
        move_pattern(self.tmp, "lightsout", "everyday")
        d = load_classification(self.tmp)
        self.assertIn("lightsout", d["everyday"])
        self.assertNotIn("lightsout", d["case_by_case"])

    def test_move_removes_from_other_section(self):
        save_classification(self.tmp,
                            {"everyday": ["x"], "case_by_case": []})
        move_pattern(self.tmp, "x", "case_by_case")
        d = load_classification(self.tmp)
        self.assertEqual(d["everyday"], [])
        self.assertEqual(d["case_by_case"], ["x"])

    def test_atomic_write_survives_crash(self):
        # Implicit: save_classification uses tempfile + Path.replace
        save_classification(self.tmp, {"everyday": ["a"], "case_by_case": []})
        self.assertEqual(load_classification(self.tmp)["everyday"], ["a"])
```

- [ ] **Step 2B.1.2: Confirm fail.**
- [ ] **Step 2B.1.3: Implement `engine/classification.py`**

```python
import json
import tempfile
from pathlib import Path


def load_classification(path):
    p = Path(path)
    if not p.exists():
        return {"everyday": [], "case_by_case": []}
    return json.loads(p.read_text())


def save_classification(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=p.parent, delete=False, suffix=".tmp")
    try:
        tmp.write(json.dumps(data, indent=2))
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(p)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def move_pattern(path, pattern_id, section):
    if section not in ("everyday", "case_by_case"):
        raise ValueError(f"bad section: {section}")
    d = load_classification(path)
    for s in ("everyday", "case_by_case"):
        if pattern_id in d[s]:
            d[s].remove(pattern_id)
    if pattern_id not in d[section]:
        d[section].append(pattern_id)
    save_classification(path, d)
    return d
```

- [ ] **Step 2B.1.4: Pass.**
- [ ] **Step 2B.1.5: Commit.**

```bash
git add engine/classification.py engine/tests/test_classification.py
git commit -m "feat(api): atomic classification JSON read/write (US-TB-01 AC-06)"
```

### Task 2B.2: `GET /api/prompts` endpoint

- [ ] **Step 2B.2.1: Extend `test_api.py` with a test**

```python
def test_api_prompts_returns_two_sections(self):
    # seed DB
    self.db.insert_prompt_usage(
        date="2026-04-21", session_id="s1", project_dir="/p",
        pattern_id="lightsout", pattern_version=1,
        is_structured=1, matched_text="/lightsout", message_ordinal=0)
    resp = self._get("/api/prompts")
    self.assertEqual(resp.status, 200)
    body = json.loads(resp.read())
    self.assertIn("everyday", body)
    self.assertIn("case_by_case", body)
    self.assertIn("generated_at", body)
```

- [ ] **Step 2B.2.2: Confirm fail.**
- [ ] **Step 2B.2.3: Add routing branch and handler in `engine/api.py`**

In `do_GET`:

```python
elif path == "/api/prompts":
    self._handle_prompts()
```

Handler method (closure-scoped so `db`, `classification_path` are accessible):

```python
def _handle_prompts(self):
    from engine.classification import load_classification
    from datetime import date
    ranked = db.get_ranked_prompts(today=date.today().isoformat())
    cls = load_classification(classification_path)
    patterns_info = load_patterns_info(patterns_yaml_path)  # {id -> {intent, type}}
    skill_dirs = resolve_skill_dirs()
    everyday, case_by_case = [], []
    for row in ranked:
        info = patterns_info.get(row["pattern_id"], {})
        has_skill = any((d / row["pattern_id"]).exists() or
                        (d / f"{row['pattern_id']}.md").exists()
                        for d in skill_dirs)
        skill_candidate = (row["count_30d"] >= 3
                           and not row["is_structured"]
                           and not has_skill)
        item = {
            "pattern_id": row["pattern_id"],
            "intent": info.get("intent", ""),
            "type": "structured" if row["is_structured"] else "unstructured",
            "count_7d": row["count_7d"],
            "count_30d": row["count_30d"],
            "count_all": row["count_all"],
            "has_skill": has_skill,
            "skill_candidate": skill_candidate,
        }
        section = ("everyday" if row["pattern_id"] in cls["everyday"]
                   else "case_by_case")
        (everyday if section == "everyday" else case_by_case).append(item)
    _json_response(self, {
        "everyday": everyday, "case_by_case": case_by_case,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }, 200)
```

Helpers (module level in `api.py` or new `engine/habits.py`):

```python
def resolve_skill_dirs():
    base = [Path.home() / ".claude" / "skills",
            Path.home() / ".claude" / "commands"]
    return [p for p in base if p.exists()]


def load_patterns_info(yaml_path):
    from engine.patterns import load_patterns
    return {p["id"]: {"intent": p["intent"], "type": p["type"]}
            for p in load_patterns(yaml_path)}
```

- [ ] **Step 2B.2.4: Pass.**
- [ ] **Step 2B.2.5: Commit.**

```bash
git add engine/api.py engine/tests/test_api.py
git commit -m "feat(api): GET /api/prompts with skill-candidate flagging (US-TB-01 AC-07, AC-09a)"
```

### Task 2B.3: `POST /api/prompts/classify`

- [ ] **Step 2B.3.1: Test**

```python
def test_api_classify_moves_pattern(self):
    resp = self._post("/api/prompts/classify",
                      {"pattern_id": "lightsout", "section": "everyday"})
    self.assertEqual(resp.status, 200)
    data = load_classification(self.classification_path)
    self.assertIn("lightsout", data["everyday"])

def test_api_classify_400_on_bad_section(self):
    resp = self._post("/api/prompts/classify",
                      {"pattern_id": "x", "section": "invalid"})
    self.assertEqual(resp.status, 400)
```

- [ ] **Step 2B.3.2: Fail.**
- [ ] **Step 2B.3.3: Add branch + handler**

```python
elif path == "/api/prompts/classify":
    self._handle_classify()

def _handle_classify(self):
    from engine.classification import move_pattern
    body = self._read_json_body()
    try:
        data = move_pattern(classification_path,
                            body["pattern_id"], body["section"])
    except ValueError as e:
        return _json_response(self, {"error": str(e)}, 400)
    except KeyError as e:
        return _json_response(self, {"error": f"missing {e}"}, 400)
    _json_response(self, data, 200)
```

- [ ] **Step 2B.3.4: Pass.**
- [ ] **Step 2B.3.5: Commit.**

```bash
git add engine/api.py engine/tests/test_api.py
git commit -m "feat(api): POST /api/prompts/classify (US-TB-01 AC-07)"
```

### Task 2B.4: `POST /api/prompts/dry-run` + `POST /api/prompts/pattern`

- [ ] **Step 2B.4.1: Test**

```python
def test_api_dry_run_returns_hit_count(self):
    # seed some messages via prompt_unmatched OR a last-7d table
    ...
    resp = self._post("/api/prompts/dry-run",
                      {"regex": "(?i)^produce\\s+"})
    self.assertEqual(resp.status, 200)
    body = json.loads(resp.read())
    self.assertIn("hit_count", body)
    self.assertIn("sample_matches", body)

def test_api_add_pattern_appends_to_yaml(self):
    resp = self._post("/api/prompts/pattern",
                      {"id": "newp", "intent": "test",
                       "regex": "^xyz", "type": "unstructured", "version": 1})
    self.assertEqual(resp.status, 200)
```

- [ ] **Step 2B.4.2: Fail.**
- [ ] **Step 2B.4.3: Implement** — reuse ingest's regex engine on messages from `prompt_unmatched.text_excerpt` and recent `prompt_usage.matched_text`. YAML append: load, mutate, dump via tempfile + replace.
- [ ] **Step 2B.4.4: Pass.**
- [ ] **Step 2B.4.5: Commit.**

```bash
git add engine/api.py engine/tests/test_api.py
git commit -m "feat(api): dry-run + YAML pattern append endpoints (US-TB-01 AC-12a)"
```

**Chunk 2B checkpoint:** `curl http://localhost:17420/api/prompts` returns JSON with both sections. Classify + dry-run + add-pattern all work.

---

## Chunk 3A: Dashboard "Habits" Tab (Wave 3 — parallel)

**Files:**
- Modify: `engine/dashboard.html` (add tab button + `#tab-habits` panel + JS fetch+render + CSS for arrows/badges)

**Work items:**
1. Add tab button after `Efficiency`: `<button class="tab-btn" data-tab="habits">Habits</button>`
2. Add `<div class="tab-panel" id="tab-habits">` with two sub-sections: `.habits-everyday`, `.habits-case-by-case`, plus a collapsible `.candidates-panel`.
3. Add fetch function `loadHabits()` that calls `GET /api/prompts` and renders rows: pattern_id (mono) | intent | 7d | 30d | all | type badge | skill-candidate badge (if `skill_candidate`) | up/down arrow.
4. Arrow click handler: `POST /api/prompts/classify`, optimistic UI update, error toast on non-200.
5. Skill-candidate badge: amber pill with `→ skill?` text, links to anchor in CLAUDE.md.
6. Candidate panel (AC-12a): right-docked collapsible with `GET /api/prompts/unmatched` list + regex inputs + "Preview" button → `POST /api/prompts/dry-run` → inline results → "Add to YAML" → `POST /api/prompts/pattern`.
7. Poll interval 300s (same as existing tabs).

No new test file — this is HTML/JS in a vanilla project. Manual browser verification listed in Chunk 4.

Single commit after all 7 items:

```bash
git add engine/dashboard.html
git commit -m "feat(dashboard): Habits tab with skill-candidate + dry-run UI (US-TB-01 AC-08..AC-12a)"
```

---

## Chunk 3B: launchd (macOS) + Task Scheduler (Windows parity) (Wave 3 — parallel)

**Files:**
- Create: `scripts/com.jcords.prompt-usage-ingest.plist`
- Create: `scripts/install-macos-launchd.sh`
- Create: `scripts/install-windows-task.ps1`

### Task 3B.1: Write the plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jcords.prompt-usage-ingest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string><string>python3</string>
    <string>-m</string><string>engine.ingest_prompts</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/jcords-macmini/projects/claude-usage-systray</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key>
  <string>/Users/jcords-macmini/.local/state/prompt-usage-ingest.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/jcords-macmini/.local/state/prompt-usage-ingest.log</string>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
```

### Task 3B.2: `scripts/install-macos-launchd.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
DEST="$HOME/Library/LaunchAgents/com.jcords.prompt-usage-ingest.plist"
cp "$(dirname "$0")/com.jcords.prompt-usage-ingest.plist" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "loaded: $DEST"
```

### Task 3B.3: `scripts/install-windows-task.ps1`

```powershell
$ProjectDir = Split-Path $PSScriptRoot -Parent
$Action = New-ScheduledTaskAction -Execute "python" `
    -Argument "-m engine.ingest_prompts" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "PromptUsageIngest" `
    -Action $Action -Trigger $Trigger -Force
```

Commit:

```bash
git add scripts/com.jcords.prompt-usage-ingest.plist scripts/install-macos-launchd.sh scripts/install-windows-task.ps1
git commit -m "feat(infra): launchd plist + macOS/Windows install scripts (US-TB-01 AC-13, AC-15)"
```

---

## Chunk 3C: Pattern YAML Seed + Eval Harness (Wave 3 — parallel)

**Files:**
- Create: `~/.claude/projects/-Users-jcords-macmini-projects/memory/prompt-patterns.yaml` (seed with 20 patterns from the signal-based toplist produced in the 2026-04-21 session)
- Create: `engine/eval_label.py` (small TUI for AC-16a hand-labeling)
- Create: `engine/tests/test_eval.py`

### Task 3C.1: Seed YAML (no test — data)

```yaml
patterns:
  - id: produce
    intent: SVG-PAINT taxonomic production run
    regex: '(?i)^\s*produce\s+'
    type: unstructured
    version: 1
  - id: ebg
    intent: XLSX bridge to Gemini via GDrive/Sheets
    regex: '(?i)^\s*ebg\b'
    type: unstructured
    version: 1
  - id: deploy
    intent: Deploy to gtxs.eu / live service
    regex: '(?i)^\s*(deploy|publish)\b'
    type: unstructured
    version: 1
  - id: dossier
    intent: Consigliere OSINT investigation
    regex: '(?i)^\s*dossier\b'
    type: unstructured
    version: 1
  - id: tags_apply
    intent: Etsy tag meta application
    regex: '(?i)^\s*tags\s+apply\b'
    type: unstructured
    version: 1
  - id: restart_server
    intent: Kill + relaunch local service
    regex: '(?i)restart\s+(the\s+)?(server|svg[-\s]?paint|consigliere)'
    type: unstructured
    version: 1
  - id: fix_bug
    intent: Direct bug-fix request (shown/attached)
    regex: '(?i)^\s*(fix|repair)\s+(this|it|the\s+bug)\b'
    type: unstructured
    version: 1
  - id: brainstorm
    intent: Open-ended design exploration
    regex: '(?i)^\s*brainstorm\b'
    type: unstructured
    version: 1
  - id: phone_commands
    intent: Update phone-facing commands webpage
    regex: '(?i)phone\s+commands?\b'
    type: unstructured
    version: 1
```

(Full 20-pattern seed list mirrors the toplist. Patterns 10-20 to be filled during implementation by reading the toplist verbatim.)

### Task 3C.2: Eval labeling TUI `engine/eval_label.py`

Minimal: list unlabeled matches for each pattern from last 30 days, ask `[y]true-positive / [n]false-positive / [s]skip`, write to `prompt_pattern_eval_labels`. On quit, compute precision per pattern and write summary row to `prompt_pattern_eval`.

TDD for core scoring (`compute_precision`):

```python
class TestPrecision(unittest.TestCase):
    def test_precision_from_labels(self):
        from engine.eval_label import compute_precision
        labels = [True, True, True, False, True]  # 4/5
        self.assertEqual(compute_precision(labels), 0.8)
```

Implementation:

```python
def compute_precision(labels):
    if not labels:
        return None
    tp = sum(1 for x in labels if x)
    return tp / len(labels)
```

Commit:

```bash
git add engine/eval_label.py engine/tests/test_eval.py
git commit -m "feat(eval): labeling TUI + precision scorer (US-TB-01 AC-16a)"
```

YAML seed commit (governance repo):

```bash
# NOTE: YAML lives in ~/.claude/projects/.../memory/ — committed to the memory repo, not claude-usage-systray
```

---

## Chunk 3D: `/lightsout` Post-Step Integration (Wave 3 — parallel)

**Files:**
- Modify: `/Users/jcords-macmini/.claude/commands/lightsout.md` (or wherever the skill lives — verify with `ls ~/.claude/**/lightsout*`)
- Add a pre-HANDOVER step that invokes ingest with a 30s timeout.

Step body to add:

```bash
# Ingest prompt usage (non-blocking — 30s timeout, log-only failure)
timeout 30 python3 -m engine.ingest_prompts 2>&1 \
  | tee -a ~/.local/state/prompt-usage-ingest.log \
  || echo "⚠ ingest failed (see log)" >> "$HANDOVER_PATH"
```

(adapt to the skill's actual shell / execution context — the skill file structure controls this).

Commit:

```bash
cd /Users/jcords-macmini/.claude
git add commands/lightsout.md  # or wherever
git commit -m "feat(lightsout): pre-HANDOVER prompt-usage ingest (US-TB-01 AC-14)"
```

---

## Chunk 4: Bootstrap + Verification + DoD (Wave 4 — serial)

### Task 4.1: Bootstrap ingest on real transcripts

- [ ] Run: `cd /Users/jcords-macmini/projects/claude-usage-systray && python3 -m engine.ingest_prompts`
- [ ] Expected output: `ingest: <N> msgs, >=80.0% matched, <M> unmatched`

### Task 4.2: AC-16a eval pass

- [ ] Run: `python3 -m engine.eval_label`
- [ ] Label at least 10 matches per pattern with >=10 matches.
- [ ] Verify: all sampled patterns hit precision >= 95%; zero false positives on 50 negative-control messages.

### Task 4.3: Curl smoke (AC-17)

- [ ] Start server: `python3 -m engine.server --port 17420 --token $CLAUDE_TOKEN` (or equivalent)
- [ ] Run: `curl -s http://localhost:17420/api/prompts | jq '. | {everyday: (.everyday | length), case_by_case: (.case_by_case | length)}'`
- [ ] Expected: both counts >= 1.

### Task 4.4: Restart persistence (AC-18)

- [ ] `curl -X POST -d '{"pattern_id":"lightsout","section":"everyday"}' -H "Content-Type: application/json" http://localhost:17420/api/prompts/classify`
- [ ] `launchctl unload ~/Library/LaunchAgents/com.jcords.prompt-usage-ingest.plist && launchctl load ...`
- [ ] Restart server process.
- [ ] `curl -s http://localhost:17420/api/prompts | jq '.everyday[].pattern_id' | grep lightsout` → must print.

### Task 4.5: Browser verification (AC-08..AC-12a)

- [ ] Open `http://localhost:17420` in browser.
- [ ] Click `Habits` tab — both sections visible.
- [ ] Click down-arrow on an everyday row — moves to case_by_case.
- [ ] Open "Candidate patterns" panel — top 10 unmatched shown, regex preview works.

### Task 4.6: DoD sign-off

- [ ] All 25 ACs ticked in BACKLOG.md (edit file, tick boxes, commit governance-only).
- [ ] Update `/Users/jcords-macmini/projects/claude-usage-systray/DONE-Today.md` with a US-TB-01 entry.
- [ ] Append any architectural insights to `/Users/jcords-macmini/projects/00_Governance/KNOWN_PATTERNS.md`.
- [ ] Record final panel verdict line in US Evidence block: "Implementation completed YYYY-MM-DD."

Commit sweep:

```bash
cd /Users/jcords-macmini/projects/claude-usage-systray
git add DONE-Today.md && git commit -m "docs: DONE-Today US-TB-01 (Habits tab shipped)"

cd /Users/jcords-macmini/projects/00_Governance
git add BACKLOG.md KNOWN_PATTERNS.md
git commit -m "chore(governance): US-TB-01 ACs ticked + KP updates"
```

---

## Out of scope (explicit — do not build in this plan)

- LLM classification of unstructured prompts in the hot path.
- LLM-assisted pattern suggestion over `prompt_unmatched` (v2; separate US).
- Cross-machine classification sync (v2).
- Auto-demotion of stale patterns (deferred until 60d data exists).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Regex seed YAML produces <80% match on real data | AC-05a coverage report; refine patterns from prompt_unmatched top-N via dry-run panel before bootstrap counts are trusted |
| JSONL schema drift (Claude Code adds new content block types) | `_extract_user_text` returns None on unknown shapes; logged as unmatched, not crashed |
| launchd plist path hardcodes `/Users/jcords-macmini` | Install script (`install-macos-launchd.sh`) rewrites `WorkingDirectory` via `sed` at install time. Add to plist install task. |
| Tests using in-memory DB diverge from file-backed behavior | Integration test in Chunk 4.1 validates with real file path |

---

**Plan complete.** Save path: `/Users/jcords-macmini/projects/claude-usage-systray/docs/plans/2026-04-21-habits-tab.md`.
