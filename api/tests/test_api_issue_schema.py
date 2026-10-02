"""`Issue` carries the body GitHub sends, and tolerates the null it sends for an empty one."""

from repolace_shared.github.schemas import Issue

from api_support import raw_issue


class TestIssueBody:
    def test_the_body_is_parsed(self):
        issue = Issue.model_validate(raw_issue(body="It crashes.\n\nTraceback follows."))

        assert issue.body == "It crashes.\n\nTraceback follows."

    def test_a_null_body_is_accepted_because_that_is_how_github_sends_an_empty_one(self):
        issue = Issue.model_validate(raw_issue(body=None))

        assert issue.body is None

    def test_a_missing_body_key_is_accepted(self):
        payload = raw_issue()
        del payload["body"]

        assert Issue.model_validate(payload).body is None

    def test_an_empty_string_body_is_kept_as_an_empty_string(self):
        """Distinct from null: the model must not normalise one into the other."""
        assert Issue.model_validate(raw_issue(body="")).body == ""

    def test_the_other_fields_still_parse_beside_it(self):
        issue = Issue.model_validate(raw_issue(number=12, title="Boom"))

        assert (issue.number, issue.title, issue.state) == (12, "Boom", "open")
        assert issue.is_pull_request is False
