from feishu_agent.runtime.message_deduper import MessageDeduper


def test_message_deduper_ignores_completed_duplicates():
    deduper = MessageDeduper(ttl_seconds=60)

    assert deduper.should_process("evt-1") is True

    deduper.mark_finished("evt-1", keep=True)

    assert deduper.should_process("evt-1") is False


def test_message_deduper_allows_retry_when_previous_attempt_not_kept():
    deduper = MessageDeduper(ttl_seconds=60)

    assert deduper.should_process("evt-2") is True

    deduper.mark_finished("evt-2", keep=False)

    assert deduper.should_process("evt-2") is True
