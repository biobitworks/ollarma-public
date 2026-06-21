"""Tests for harness/plan_parser.py — GSD PLAN.md file parser.

TDD RED: These tests import from ollarma.plan_parser which does not exist yet.
All tests will fail with ImportError until harness/plan_parser.py is created.
"""
import pytest

from ollarma.plan_parser import PlanTask, PlanMetadata, parse_plan_tasks


# ---------------------------------------------------------------------------
# Minimal but complete PLAN.md fixture — mirrors real 07-01-PLAN.md format
# ---------------------------------------------------------------------------

PLAN_MD_FIXTURE = """\
---
phase: 07-sonified-evidence-review
plan: 01
type: tdd
wave: 1
depends_on: []
files_modified:
  - harness/sonifier.py
  - tests/test_sonifier.py
autonomous: true
requirements:
  - SONI-01
  - SONI-02
---

<objective>
Build the core sonifier module as pure functions with TDD.
</objective>

<context>
@.planning/PROJECT.md
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Write failing tests (TDD RED)</name>
  <files>tests/test_sonifier.py</files>
  <read_first>
    - harness/evidence.py
    - tests/test_evidence.py
  </read_first>
  <action>
Create tests/test_sonifier.py with the following test structure:

```python
def test_example():
    assert True
```

Make sure the tests import from ollarma.sonifier.
  </action>
  <acceptance_criteria>
    - tests/test_sonifier.py exists with at least 10 test methods
    - python -m pytest tests/test_sonifier.py -x fails (RED)
  </acceptance_criteria>
  <done>Tests written, all fail with ImportError.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Implement sonifier module (TDD GREEN)</name>
  <files>harness/sonifier.py, tests/test_sonifier.py</files>
  <read_first>
    - harness/evidence.py
  </read_first>
  <action>
Create harness/sonifier.py following the evidence.py pattern.
  </action>
  <acceptance_criteria>
    - python -m pytest tests/test_sonifier.py -x passes (GREEN)
    - python -m pytest tests/ -x passes (full suite)
  </acceptance_criteria>
  <done>Sonifier module implemented, all tests green.</done>
</task>

</tasks>
"""


PLAN_MD_NO_TASKS = """\
---
phase: 08-local-agent-runtime
plan: 01
type: execute
wave: 1
---

<objective>
A plan with no tasks block.
</objective>
"""


PLAN_MD_EMPTY_TASKS = """\
---
phase: 08-local-agent-runtime
plan: 02
type: execute
wave: 1
---

<tasks>
</tasks>
"""


# ---------------------------------------------------------------------------
# PlanTask model
# ---------------------------------------------------------------------------


class TestPlanTask:
    """Tests for PlanTask Pydantic model."""

    def test_is_frozen_pydantic(self) -> None:
        """PlanTask has frozen=True config."""
        task = PlanTask(name="Test", action="do something")
        with pytest.raises(Exception):  # ValidationError on frozen model
            task.name = "Changed"  # type: ignore[misc]

    def test_required_fields(self) -> None:
        """PlanTask requires name and action fields."""
        task = PlanTask(name="Test task", action="do the thing")
        assert task.name == "Test task"
        assert task.action == "do the thing"

    def test_optional_fields_default(self) -> None:
        """PlanTask optional fields have sensible defaults."""
        task = PlanTask(name="Test", action="act")
        assert task.files == []
        assert task.acceptance_criteria == []
        assert task.read_first == []
        assert task.done == ""


# ---------------------------------------------------------------------------
# PlanMetadata model
# ---------------------------------------------------------------------------


class TestPlanMetadata:
    """Tests for PlanMetadata Pydantic model."""

    def test_parses_yaml_frontmatter(self) -> None:
        """PlanMetadata fields populated from YAML frontmatter."""
        metadata, _ = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert metadata.phase == "07-sonified-evidence-review"
        assert metadata.plan == 1
        assert metadata.type == "tdd"

    def test_extracts_requirements(self) -> None:
        """requirements field is list of strings from YAML."""
        metadata, _ = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert "SONI-01" in metadata.requirements
        assert "SONI-02" in metadata.requirements

    def test_extracts_wave(self) -> None:
        """wave field parsed from YAML frontmatter."""
        metadata, _ = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert metadata.wave == 1

    def test_extracts_files_modified(self) -> None:
        """files_modified field parsed from YAML frontmatter."""
        metadata, _ = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert "harness/sonifier.py" in metadata.files_modified
        assert "tests/test_sonifier.py" in metadata.files_modified


# ---------------------------------------------------------------------------
# parse_plan_tasks
# ---------------------------------------------------------------------------


class TestParsePlanTasks:
    """Tests for parse_plan_tasks function."""

    def test_parses_real_plan_format(self) -> None:
        """parse_plan_tasks returns metadata and list of PlanTask from fixture."""
        metadata, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert isinstance(metadata, PlanMetadata)
        assert isinstance(tasks, list)
        assert len(tasks) == 2

    def test_extracts_task_name(self) -> None:
        """First task name starts with 'Task 1:'."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert tasks[0].name.startswith("Task 1:")

    def test_extracts_action_text(self) -> None:
        """action field contains multi-line text."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert "test_sonifier" in tasks[0].action
        assert len(tasks[0].action) > 20

    def test_extracts_files(self) -> None:
        """files field is a list of file paths."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert "tests/test_sonifier.py" in tasks[0].files

    def test_extracts_acceptance_criteria(self) -> None:
        """acceptance_criteria is a list of strings."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        criteria = tasks[0].acceptance_criteria
        assert isinstance(criteria, list)
        assert len(criteria) >= 1
        assert any("test_sonifier" in c for c in criteria)

    def test_handles_code_blocks_in_action(self) -> None:
        """action with ``` code blocks does not crash the parser."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        # The first task has a code block in its action
        assert "def test_example" in tasks[0].action

    def test_empty_tasks_block(self) -> None:
        """Plan with empty tasks block returns empty list."""
        _, tasks = parse_plan_tasks(PLAN_MD_EMPTY_TASKS)
        assert tasks == []

    def test_no_tasks_block(self) -> None:
        """Plan with no tasks XML returns empty list."""
        _, tasks = parse_plan_tasks(PLAN_MD_NO_TASKS)
        assert tasks == []

    def test_extracts_read_first(self) -> None:
        """read_first field is a list of file paths."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert "harness/evidence.py" in tasks[0].read_first

    def test_extracts_done(self) -> None:
        """done field contains summary text."""
        _, tasks = parse_plan_tasks(PLAN_MD_FIXTURE)
        assert "ImportError" in tasks[0].done

    def test_nested_closing_tag_truncation_known_limitation(self) -> None:
        """WR-04: action containing </tasks> literal truncates silently.

        This is a known limitation of regex-based XML parsing (P-08-02
        mitigation). The non-greedy match terminates at the first </tasks>
        occurrence even if it's inside action content. This test documents
        the expected (degraded) behavior.
        """
        plan_with_nested_tag = """\
---
phase: test
plan: 1
type: execute
---

<tasks>

<task type="auto">
  <name>Task 1: Generate XML</name>
  <files>output.xml</files>
  <action>
Write code that produces:
```xml
</tasks>
```
Then validate the output.
  </action>
  <acceptance_criteria>
    - output.xml is valid
  </acceptance_criteria>
</task>

<task type="auto">
  <name>Task 2: This task is lost</name>
  <files>lost.py</files>
  <action>This should be parsed but is dropped due to truncation.</action>
</task>

</tasks>
"""
        _, tasks = parse_plan_tasks(plan_with_nested_tag)
        # Known limitation: Task 2 is silently dropped because the regex
        # <tasks>(.*?)</tasks> terminates at the </tasks> inside the code block.
        # This test documents the behavior -- it is NOT a test of correct parsing.
        assert len(tasks) <= 1  # Task 2 lost due to truncation
