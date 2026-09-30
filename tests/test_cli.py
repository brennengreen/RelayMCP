def test_setup_success_invites_feedback_and_support(capsys):
    from relaymcp.host import cli

    cli._print_setup_success()

    output = capsys.readouterr().out
    assert "Take a screenshot of my handheld" in output
    assert "https://github.com/brennengreen/RelayMCP/discussions/5" in output
    assert "Star RelayMCP to help other handheld owners find it" in output
