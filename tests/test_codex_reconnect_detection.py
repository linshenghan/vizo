from vizo_core.agent_runner import _is_codex_reconnect_exhausted_event


def test_codex_reconnect_detection_waits_until_final_attempt():
    assert not _is_codex_reconnect_exhausted_event(
        {
            "type": "error",
            "message": "Reconnecting... 4/5 (timeout waiting for child process to exit)",
        }
    )


def test_codex_reconnect_detection_matches_final_attempt():
    assert _is_codex_reconnect_exhausted_event(
        {
            "type": "error",
            "message": "Reconnecting... 5/5 (timeout waiting for child process to exit)",
        }
    )
