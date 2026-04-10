import json
from unittest.mock import patch, MagicMock, call

from autocoder.epic import (
    process_epic,
    update_epic_checkbox,
    close_epic,
    comment_epic_progress,
    _MAX_EPIC_DEPTH,
)
from autocoder.issues import parse_sub_issues
from autocoder.types import EpicResult, Issue, Priority, is_epic


# ---------------------------------------------------------------------------
# is_epic
# ---------------------------------------------------------------------------


def test_is_epic_with_epic_label():
    issue = Issue(1, "Epic", "", ["epic"], Priority.P3, "")
    assert is_epic(issue) is True


def test_is_epic_with_meta_label():
    issue = Issue(1, "Meta", "", ["meta", "enhancement"], Priority.P3, "")
    assert is_epic(issue) is True


def test_is_epic_with_tracking_label():
    issue = Issue(1, "Track", "", ["Tracking"], Priority.P3, "")
    assert is_epic(issue) is True


def test_is_epic_without_label():
    issue = Issue(1, "Bug", "", ["bug"], Priority.P0, "")
    assert is_epic(issue) is False


def test_is_epic_case_insensitive():
    issue = Issue(1, "Epic", "", ["EPIC"], Priority.P3, "")
    assert is_epic(issue) is True


# ---------------------------------------------------------------------------
# parse_sub_issues
# ---------------------------------------------------------------------------


def test_parse_sub_issues_checkbox():
    body = "- [ ] #123 fix login\n- [x] #456 add tests"
    assert parse_sub_issues(body) == [123, 456]


def test_parse_sub_issues_checkbox_star():
    body = "* [ ] #10 task one\n* [X] #20 task two"
    assert parse_sub_issues(body) == [10, 20]


def test_parse_sub_issues_url():
    body = "- [ ] https://github.com/org/repo/issues/789 some task"
    assert parse_sub_issues(body) == [789]


def test_parse_sub_issues_bare_ref():
    body = "- #42 do something\n- #43 another thing"
    assert parse_sub_issues(body) == [42, 43]


def test_parse_sub_issues_mixed():
    body = (
        "## Tasks\n"
        "- [ ] #1 first task\n"
        "- [x] #2 done task\n"
        "- #3 bare ref\n"
        "- [ ] https://github.com/org/repo/issues/4 url ref\n"
    )
    result = parse_sub_issues(body)
    assert set(result) == {1, 2, 3, 4}
    assert len(result) == 4


def test_parse_sub_issues_empty_body():
    assert parse_sub_issues("") == []


def test_parse_sub_issues_no_references():
    assert parse_sub_issues("This is just text with no issue refs.") == []


def test_parse_sub_issues_deduplication():
    body = "- [ ] #5 task\n- [x] #5 same task"
    assert parse_sub_issues(body) == [5]


def test_parse_sub_issues_preserves_order():
    body = "- [ ] #30 third\n- [ ] #10 first\n- [ ] #20 second"
    assert parse_sub_issues(body) == [30, 10, 20]


# ---------------------------------------------------------------------------
# update_epic_checkbox
# ---------------------------------------------------------------------------


@patch("autocoder.epic.subprocess.run")
def test_update_epic_checkbox(mock_run):
    body = "- [ ] #5 task one\n- [ ] #10 task two"
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout=json.dumps({"body": body})),  # view
        MagicMock(returncode=0),  # edit
    ]
    update_epic_checkbox("/repo", 1, 5)
    assert mock_run.call_count == 2
    edit_call = mock_run.call_args_list[1]
    new_body = edit_call[0][0][-1]  # last arg is the body
    assert "- [x] #5" in new_body
    assert "- [ ] #10" in new_body


@patch("autocoder.epic.subprocess.run")
def test_update_epic_checkbox_no_match(mock_run):
    body = "- [ ] #99 unrelated"
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({"body": body}))
    update_epic_checkbox("/repo", 1, 5)
    # Only the view call, no edit since body didn't change
    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# close_epic / comment_epic_progress
# ---------------------------------------------------------------------------


@patch("autocoder.epic.subprocess.run")
def test_close_epic(mock_run):
    epic = Issue(1, "Epic", "", ["epic"], Priority.P3, "")
    result = EpicResult(
        epic_number=1, sub_issues=[5, 10],
        succeeded=[5, 10], failed=[], skipped_closed=[], all_complete=True,
    )
    close_epic("/repo", epic, result)
    assert mock_run.call_count == 2
    # First call: comment
    comment_call = mock_run.call_args_list[0][0][0]
    assert "comment" in comment_call
    # Second call: close
    close_call = mock_run.call_args_list[1][0][0]
    assert "close" in close_call


@patch("autocoder.epic.subprocess.run")
def test_comment_epic_progress(mock_run):
    epic = Issue(1, "Epic", "", ["epic"], Priority.P3, "")
    result = EpicResult(
        epic_number=1, sub_issues=[5, 10],
        succeeded=[5], failed=[10], skipped_closed=[], all_complete=False,
    )
    comment_epic_progress("/repo", epic, result)
    assert mock_run.call_count == 1
    body_arg = mock_run.call_args[0][0]
    assert "comment" in body_arg


# ---------------------------------------------------------------------------
# process_epic
# ---------------------------------------------------------------------------


def _make_cfg(repo_path="/repo"):
    return MagicMock(repo_path=repo_path)


def _make_epic(number=1, body="- [ ] #5\n- [ ] #10"):
    return Issue(number, "Test Epic", body, ["epic"], Priority.P3, "")


@patch("autocoder.epic.close_epic")
@patch("autocoder.epic.update_epic_checkbox")
@patch("autocoder.epic.fetch_sub_issues")
@patch("autocoder.loop.process_issue")
def test_process_epic_all_success(mock_process, mock_fetch, mock_checkbox, mock_close):
    epic = _make_epic()
    sub5 = Issue(5, "Sub 5", "", [], Priority.P0, "")
    sub10 = Issue(10, "Sub 10", "", [], Priority.P1, "")
    mock_fetch.return_value = ([sub5, sub10], [])
    budget = MagicMock()
    budget.daily_exhausted.return_value = False

    result = process_epic(epic, _make_cfg(), MagicMock(), budget, MagicMock(), MagicMock(), MagicMock())

    assert result.all_complete is True
    assert result.succeeded == [5, 10]
    assert result.failed == []
    assert mock_process.call_count == 2
    assert mock_checkbox.call_count == 2
    mock_close.assert_called_once()


@patch("autocoder.epic.comment_epic_progress")
@patch("autocoder.epic.update_epic_checkbox")
@patch("autocoder.epic.fetch_sub_issues")
@patch("autocoder.loop.process_issue")
def test_process_epic_partial_failure(mock_process, mock_fetch, mock_checkbox, mock_comment):
    epic = _make_epic()
    sub5 = Issue(5, "Sub 5", "", [], Priority.P0, "")
    sub10 = Issue(10, "Sub 10", "", [], Priority.P1, "")
    mock_fetch.return_value = ([sub5, sub10], [])
    mock_process.side_effect = [None, RuntimeError("failed")]
    budget = MagicMock()
    budget.daily_exhausted.return_value = False

    result = process_epic(epic, _make_cfg(), MagicMock(), budget, MagicMock(), MagicMock(), MagicMock())

    assert result.all_complete is False
    assert result.succeeded == [5]
    assert result.failed == [10]
    mock_comment.assert_called_once()


@patch("autocoder.epic.fetch_sub_issues")
def test_process_epic_no_sub_issues(mock_fetch):
    epic = Issue(1, "Empty Epic", "No tasks here", ["epic"], Priority.P3, "")
    budget = MagicMock()

    result = process_epic(epic, _make_cfg(), MagicMock(), budget, MagicMock(), MagicMock(), MagicMock())

    assert result.all_complete is True
    assert result.sub_issues == []
    mock_fetch.assert_not_called()


@patch("autocoder.epic.close_epic")
@patch("autocoder.epic.fetch_sub_issues")
def test_process_epic_all_already_closed(mock_fetch, mock_close):
    epic = _make_epic()
    mock_fetch.return_value = ([], [5, 10])  # all closed
    budget = MagicMock()
    budget.daily_exhausted.return_value = False

    result = process_epic(epic, _make_cfg(), MagicMock(), budget, MagicMock(), MagicMock(), MagicMock())

    assert result.all_complete is True
    assert result.skipped_closed == [5, 10]
    mock_close.assert_called_once()


@patch("autocoder.epic.comment_epic_progress")
@patch("autocoder.epic.fetch_sub_issues")
@patch("autocoder.loop.process_issue")
def test_process_epic_nested_depth_guard(mock_process, mock_fetch, mock_comment):
    epic = _make_epic(body="- [ ] #5")
    nested_epic = Issue(5, "Nested Epic", "- [ ] #20", ["epic"], Priority.P3, "")
    mock_fetch.return_value = ([nested_epic], [])
    budget = MagicMock()
    budget.daily_exhausted.return_value = False

    result = process_epic(
        epic, _make_cfg(), MagicMock(), budget, MagicMock(), MagicMock(), MagicMock(),
        depth=_MAX_EPIC_DEPTH,
    )

    assert result.failed == [5]
    mock_process.assert_not_called()


@patch("autocoder.epic.comment_epic_progress")
@patch("autocoder.epic.update_epic_checkbox")
@patch("autocoder.epic.fetch_sub_issues")
@patch("autocoder.loop.process_issue")
def test_process_epic_budget_exhausted_stops(mock_process, mock_fetch, mock_checkbox, mock_comment):
    epic = _make_epic()
    sub5 = Issue(5, "Sub 5", "", [], Priority.P0, "")
    sub10 = Issue(10, "Sub 10", "", [], Priority.P1, "")
    mock_fetch.return_value = ([sub5, sub10], [])
    budget = MagicMock()
    budget.daily_exhausted.side_effect = [False, True]  # exhausted after first

    result = process_epic(epic, _make_cfg(), MagicMock(), budget, MagicMock(), MagicMock(), MagicMock())

    assert mock_process.call_count == 1
    assert result.succeeded == [5]
