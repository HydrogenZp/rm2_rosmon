from rosmon2.model import ProcessRecord, ProcessState, selection_key
from rosmon2.registry import ProcessRegistry


def test_selection_keys_match_rosmon_order():
    assert selection_key(0) == 'a'
    assert selection_key(25) == 'z'
    assert selection_key(26) == 'A'
    assert selection_key(51) == 'Z'
    assert selection_key(52) == '0'
    assert selection_key(61) == '9'
    assert selection_key(62) is None


def test_process_record_exposes_explicit_lifecycle_fields():
    record = ProcessRecord(key=0, display_name='robot/driver')

    assert record.name == 'robot/driver'
    assert record.namespace == '/'
    assert record.state is ProcessState.STOPPED
    assert record.exit_code is None
    assert record.expected_stop is False


def test_registry_keeps_action_association_without_monkey_patching():
    registry = ProcessRegistry()
    record = registry.create('robot/driver', 'robot')
    action = object()

    registry.bind(action, record)

    assert registry.by_action(action) is record
    assert record.action is action
    registry.unbind(action)
    assert registry.by_action(action) is None
