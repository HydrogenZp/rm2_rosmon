import os

from rosmon2.model import ProcessRecord, State
from rosmon2.terminal import ANSI_RE, _hsluv_label_color, TerminalUI


def test_ros_severity_takes_precedence_over_stderr_channel():
    assert TerminalUI._severity('[INFO] node started', None, True) == 'INFO'
    assert TerminalUI._severity('[WARN] delayed', None, False) == 'WARNING'
    assert TerminalUI._severity('plain stderr', None, True) == 'ERROR'


def test_ros_console_metadata_keeps_last_context_field():
    line = (
        '[ERROR] [1784782198.813074890] [ur10e.robot_state_receiver]: '
        'Cannot find any device'
    )

    assert TerminalUI._message_body(line) == (
        '[ur10e.robot_state_receiver]: Cannot find any device'
    )
    assert TerminalUI._message_body(
        '[INFO] [core::RobotModel::buildModel]: Loading model'
    ) == '[core::RobotModel::buildModel]: Loading model'
    assert TerminalUI._message_body(
        '[WARN] []: Function unavailable'
    ) == '[]: Function unavailable'
    assert TerminalUI._message_body('[WARN] message without metadata') == (
        '[WARN] message without metadata'
    )
    assert TerminalUI._message_body('\x1b[36mcolored message\x1b[0m') == (
        '\x1b[36mcolored message\x1b[0m'
    )


def test_log_row_keeps_process_label_context_and_message(capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.records = [
        ProcessRecord(key=0, display_name='ur10e/robot_state_receiver'),
    ]

    ui.log(
        'ur10e/robot_state_receiver',
        '[INFO] [1784782198.813074890] [receiver]: Device connected',
    )

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert output == (
        'ur10e/robot_state_receiver: [receiver]: Device connected\n'
    )
    assert '[INFO]' not in output
    assert '[1784782198.813074890]' not in output
    assert '[receiver]' in output


def test_processes_get_distinct_stable_label_colors():
    ui = TerminalUI(False, lambda _key: None)
    ui.records = [
        ProcessRecord(key=0, display_name='/talker'),
        ProcessRecord(key=1, display_name='/listener'),
    ]
    assert ui._label_color('/talker') != ui._label_color('/listener')
    assert ui._label_color('/talker') == ui._label_color('/talker')
    assert ui._label_color('launch') is None


def test_process_label_cache_rebuilds_only_when_names_change(monkeypatch):
    color_calls = []

    def fake_color(hue):
        color_calls.append(hue)
        return (int(hue), 1, 2)

    monkeypatch.setattr('rosmon2.terminal._hsluv_label_color', fake_color)
    ui = TerminalUI(False, lambda _key: None)
    records = [
        ProcessRecord(key=0, display_name='talker'),
        ProcessRecord(key=1, display_name='robot/long_listener'),
    ]

    ui.set_records(records)
    first_label = ui._styled_label('talker', ui._label_width)
    assert len(ANSI_RE.sub('', first_label)) == ui._label_width + 1
    assert len(color_calls) == 2

    records[0].state = State.RUNNING
    ui.set_records(records)
    assert ui._styled_label('talker', ui._label_width) is first_label
    assert len(color_calls) == 2

    records.append(ProcessRecord(key=2, display_name='relay'))
    ui.set_records(records)
    assert len(color_calls) == 5


def test_hsluv_colors_match_rosmon_reference_palette():
    assert _hsluv_label_color(0) == (102, 0, 39)
    assert _hsluv_label_color(120) == (21, 55, 0)
    assert _hsluv_label_color(240) == (0, 51, 78)


def test_status_colors_match_rosmon_reference_palette():
    assert '\x1b[48;2;0;64;64m' in TerminalUI.BAR
    assert '\x1b[48;2;0;96;96m' in TerminalUI.BAR_KEY
    assert '\x1b[48;2;200;200;200m' in TerminalUI.KEY
    assert '\x1b[48;2;24;178;24m' in TerminalUI.RUNNING
    assert '\x1b[48;2;200;200;0m' in TerminalUI.PARTIAL
    assert '\x1b[48;2;135;206;250m' in TerminalUI.NODE_SELECTED


def test_agent_created_node_uses_orange_background(monkeypatch, capsys):
    record = ProcessRecord(
        key=0,
        display_name='tools/health_probe',
        state=State.RUNNING,
        agent_created=True,
    )
    ui = TerminalUI(False, lambda _key: None)
    assert '\x1b[48;2;255;165;0m' in ui.record_style(record)

    ui.enabled = True
    ui._started = True
    ui.records = [record]
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((100, 24)),
    )

    ui.redraw()

    output = capsys.readouterr().out
    assert TerminalUI.AGENT_CREATED in output
    assert 'tools/health_probe' in ANSI_RE.sub('', output)

    for state in (State.IDLE, State.WAITING, State.CRASHED):
        record.state = state
        assert ui.record_style(record) == ui.state_style(state)
        assert ui.record_style(record) != ui.AGENT_CREATED


def test_bottom_bar_uses_rosmon_reference_colors():
    assert '48;2;0;64;64' in TerminalUI.BAR
    assert '48;2;0;96;96' in TerminalUI.BAR_KEY
    assert '48;2;200;200;200' in TerminalUI.KEY
    assert '48;2;24;178;24' in TerminalUI.RUNNING


def test_narrow_menu_preserves_key_background_colors():
    ui = TerminalUI(False, lambda _key: None)
    menu = ui._menu_item('A-Z', 'Node select') + ui._menu_item('F6', 'Start all')
    fitted = ui._fit(menu, 20)
    assert TerminalUI.BAR_KEY in fitted
    assert TerminalUI.BAR in fitted
    assert ui._visible_len(fitted) == 20


def test_status_bar_shows_complete_process_names(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.records = [
        ProcessRecord(key=0, display_name='hardware_setup'),
        ProcessRecord(key=1, display_name='ur10e/ur_ros_rtde/robot_state_receiver'),
    ]
    monkeypatch.setattr('rosmon2.terminal.shutil.get_terminal_size',
                        lambda _fallback: os.terminal_size((50, 24)))

    ui.redraw()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert 'hardware_setup' in output
    assert 'ur10e/ur_ros_rtde/robot_state_receiver' in output
    assert 'ardware_setup' not in output.replace('hardware_setup', '')


def test_batched_log_output_reprints_status_without_a_flash(
        monkeypatch, capsys):
    class FakeTimer:
        def __init__(self, callback):
            self.callback = callback
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeLoop:
        def __init__(self):
            self.timers = []

        def call_later(self, _delay, callback):
            timer = FakeTimer(callback)
            self.timers.append(timer)
            return timer

    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui._loop = FakeLoop()
    ui._last_redraw_at = 10.0
    ui.records = [
        ProcessRecord(key=0, display_name='robot/driver', state=State.RUNNING),
    ]
    monkeypatch.setattr('rosmon2.terminal.time.monotonic', lambda: 10.01)
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((100, 24)),
    )
    ui.set_records(ui.records)
    capsys.readouterr()
    ui._last_redraw_at = 10.0

    ui.log('robot/driver', '[INFO] [callback]: first')
    ui.log('robot/driver', '[INFO] [callback]: second')

    output = capsys.readouterr().out
    assert output == ''
    assert len(ui._loop.timers) == 1

    ui._loop.timers[0].callback()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert '[callback]: first' in output
    assert '[callback]: second' in output
    assert '▂' in output
    assert len(ui._loop.timers) == 1
    assert ui._redraw_timer is None

    record = ui.records[0]
    record.state = State.CRASHED
    ui.set_records(ui.records)

    output = capsys.readouterr().out
    assert '▂' in output
    assert ui._redraw_timer is None


def test_close_drains_batched_output(monkeypatch, capsys):
    class FakeTimer:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeLoop:
        def __init__(self):
            self.timers = []

        def call_later(self, _delay, _callback):
            timer = FakeTimer()
            self.timers.append(timer)
            return timer

    ui = TerminalUI(False, lambda _key: None)
    loop = FakeLoop()
    ui.start(loop)

    ui.log('driver', '[INFO] [callback]: final message')
    assert capsys.readouterr().out == ''

    ui.close()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert output == '  driver: [callback]: final message\n'
    assert loop.timers[0].cancelled


def test_unchanged_status_render_is_reused(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.records = [
        ProcessRecord(key=0, display_name='robot/driver', state=State.RUNNING),
    ]
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((100, 24)),
    )
    visible_len_calls = []
    original_visible_len = ui._visible_len
    monkeypatch.setattr(
        ui,
        '_visible_len',
        lambda text: visible_len_calls.append(text) or original_visible_len(text),
    )

    ui.redraw()
    assert visible_len_calls
    visible_len_calls.clear()

    ui.redraw()

    capsys.readouterr()
    assert visible_len_calls == []


def test_redraw_erases_and_replaces_status_in_one_write(monkeypatch):
    class Output:
        def __init__(self):
            self.writes = []

        def write(self, text):
            self.writes.append(text)

        def flush(self):
            pass

    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui._status_lines = 2
    ui.records = [
        ProcessRecord(key=0, display_name='robot/driver', state=State.RUNNING),
    ]
    output = Output()
    monkeypatch.setattr('rosmon2.terminal.sys.stdout', output)
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((100, 24)),
    )

    ui.redraw()

    assert len(output.writes) == 1
    assert output.writes[0].startswith('\r\x1b[J')
    assert 'robot/driver' in output.writes[0]


def test_status_returns_to_its_origin_after_rendering():
    status = TerminalUI._status_text(('first', 'second'))

    assert status == 'first\nsecond\n\x1b[2A\r'


def test_resize_is_debounced_then_rebuilds_only_the_footer(monkeypatch, capsys):
    class FakeTimer:
        def __init__(self, callback):
            self.callback = callback
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeLoop:
        def __init__(self):
            self.timers = []

        def call_later(self, _delay, callback):
            timer = FakeTimer(callback)
            self.timers.append(timer)
            return timer

    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui._loop = FakeLoop()
    ui._status_lines = 4
    ui.records = [
        ProcessRecord(key=0, display_name='robot/driver', state=State.RUNNING),
    ]
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((80, 24)),
    )

    ui._schedule_resize_redraw()
    first = ui._loop.timers[-1]
    ui._schedule_resize_redraw()
    second = ui._loop.timers[-1]

    assert first.cancelled
    second.callback()

    output = capsys.readouterr().out
    assert output.startswith('\r\x1b[J')
    assert '\x1b[2J' not in output
    assert '\x1b[H' not in output
    assert output.count('robot/driver') == 1


def test_footer_leaves_last_terminal_column_unused(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.records = [
        ProcessRecord(key=0, display_name='robot/driver', state=State.RUNNING),
    ]
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((20, 24)),
    )

    ui.redraw()
    capsys.readouterr()

    assert all(ui._visible_len(line) <= 19 for line in ui._render_cache_lines)
    assert ui._visible_len(ui._render_cache_lines[0]) == 19


def test_selected_node_uses_light_blue_background(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.selected = 0
    ui.records = [
        ProcessRecord(key=0, display_name='ur10e/command_server',
                      state=State.RUNNING),
    ]
    monkeypatch.setattr('rosmon2.terminal.shutil.get_terminal_size',
                        lambda _fallback: os.terminal_size((100, 24)))

    ui.redraw()

    output = capsys.readouterr().out
    assert TerminalUI.NODE_SELECTED + '[ur10e/command_server]' in output


def test_namespace_mode_groups_child_namespaces_under_the_top_level():
    ui = TerminalUI(False, lambda _key: None)
    ui.records = [
        ProcessRecord(key=0, display_name='hardware_setup'),
        ProcessRecord(key=1, display_name='ur10e/move_group'),
        ProcessRecord(key=2, display_name='ur10e/ur_ros_rtde/command_server'),
        ProcessRecord(key=3, display_name='camera/image_publisher'),
    ]

    assert ui.namespaces() == ['/', 'camera', 'ur10e']
    assert [record.key for record in ui.records_in_namespace('ur10e')] == [1, 2]


def test_search_matches_full_names_including_namespaces():
    ui = TerminalUI(False, lambda _key: None)
    move_group = ProcessRecord(key=0, display_name='ur10e/move_group')
    command_server = ProcessRecord(
        key=1, display_name='ur10e/ur_ros_rtde/command_server')
    camera = ProcessRecord(key=2, display_name='camera/image_publisher')
    ui.records = [move_group, command_server, camera]

    ui.search_query = 'ur_ros_rtde'

    assert ui.search_matches() == [command_server]


def test_search_is_scoped_to_inspected_namespace():
    ui = TerminalUI(False, lambda _key: None)
    ur_camera = ProcessRecord(key=0, display_name='ur10e/camera')
    external_camera = ProcessRecord(key=1, display_name='external/camera')
    ui.records = [ur_camera, external_camera]
    ui.namespace_mode = True
    ui.namespace_inspect = 'ur10e'
    ui.search_query = 'camera'

    assert ui.search_matches() == [ur_camera]


def test_namespace_colors_reflect_alive_and_dead_counts():
    running = ProcessRecord(key=0, display_name='robot/driver', state=State.RUNNING)
    idle = ProcessRecord(key=1, display_name='robot/helper', state=State.IDLE)
    crashed = ProcessRecord(key=2, display_name='robot/camera', state=State.CRASHED)

    assert TerminalUI.namespace_counts([running, idle, crashed]) == (1, 2)
    assert TerminalUI.namespace_style([running]) == TerminalUI.RUNNING
    assert TerminalUI.namespace_style([running, idle]) == TerminalUI.PARTIAL
    assert TerminalUI.namespace_style([idle, crashed]) == TerminalUI.CRASHED


def test_namespace_status_bar_shows_root_group(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.namespace_mode = True
    ui.records = [
        ProcessRecord(key=0, display_name='hardware_setup'),
        ProcessRecord(key=1, display_name='ur10e/move_group'),
    ]
    monkeypatch.setattr('rosmon2.terminal.shutil.get_terminal_size',
                        lambda _fallback: os.terminal_size((80, 24)))

    ui.redraw()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert '/ [0:1]' in output
    assert 'ur10e [0:1]' in output
    assert 'hardware_setup' not in output


def test_selected_namespace_does_not_wrap_its_name_in_brackets(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.namespace_mode = True
    ui.selected = 0
    ui.records = [
        ProcessRecord(key=0, display_name='ur10e/move_group', state=State.RUNNING),
        ProcessRecord(key=1, display_name='ur10e/driver', state=State.CRASHED),
    ]
    monkeypatch.setattr('rosmon2.terminal.shutil.get_terminal_size',
                        lambda _fallback: os.terminal_size((100, 24)))

    ui.redraw()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert 'ur10e [1:1]' in output
    assert '[ur10e [1:1]]' not in output


def test_search_status_shows_query_and_only_matching_nodes(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.search_active = True
    ui.search_query = 'receiver'
    ui.records = [
        ProcessRecord(key=0, display_name='ur10e/robot_state_receiver'),
        ProcessRecord(key=1, display_name='ur10e/command_server'),
    ]
    monkeypatch.setattr('rosmon2.terminal.shutil.get_terminal_size',
                        lambda _fallback: os.terminal_size((100, 24)))

    ui.redraw()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert 'Searching for: receiver' in output
    assert 'ur10e/robot_state_receiver' in output
    assert 'ur10e/command_server' not in output


def test_codex_panel_is_rendered_below_node_list(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.records = [
        ProcessRecord(key=0, display_name='ur10e/driver', state=State.CRASHED),
    ]
    ui.open_codex()
    ui.add_codex_message('You', 'what is wrong?')
    ui.add_codex_message('Codex', 'Software likely: the driver exited.')
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((160, 24)),
    )

    ui.redraw()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert output.index('ur10e/driver') < output.index('Rosmon: Software likely')
    assert 'Codex (ready)' not in output
    assert 'Human: what is wrong?' in output
    assert 'You:' not in output
    assert 'what is wrong?' in output
    assert 'Software likely' in output
    assert 'Enter: ask' not in output
    assert 'Esc: close' not in output
    assert 'Codex usage remaining' not in output
    assert '--%' in output


def test_codex_usage_percentage_is_right_aligned_on_prompt_row():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_prompt = 'next question'
    ui.set_codex_usage(81)

    panel = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    input_line = panel.splitlines()[-1]

    assert '> next question' in input_line
    assert 'F11' not in input_line
    assert input_line.endswith('F2: GPT-5.5  81%')
    assert len(input_line) == 100
    assert 'Codex usage remaining' not in input_line

    ui.set_codex_usage(81, loading=True)
    loading = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert loading.splitlines()[-1].endswith('--%')


def test_codex_model_picker_selects_installed_model_and_updates_prompt():
    ui = TerminalUI(False, lambda _key: None)
    ui.set_codex_models([
        {
            'model': 'gpt-5.5',
            'display_name': 'GPT-5.5',
            'is_default': True,
        },
        {
            'model': 'gpt-5.3-codex',
            'display_name': 'GPT-5.3-Codex',
            'is_default': False,
        },
    ])

    default_panel = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert default_panel.splitlines()[-1].endswith('F2: GPT-5.5  --%')

    ui.open_codex_model_picker()
    picker = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Model' in picker
    assert 'Codex default' in picker
    assert '> GPT-5.5 (CLI default)' in picker
    assert 'GPT-5.3-Codex' in picker

    ui.move_codex_model_selection(2)
    ui.apply_codex_model_selection()

    assert ui.codex_selected_model == 'gpt-5.3-codex'
    assert ui.codex_model_picker_active
    assert ui.codex_model_picker_stage == 'access'
    access = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Access — step 2 of 3' in access
    assert 'Approve for me' in access
    assert '> Full access' in access

    ui.move_codex_model_selection(-1)
    ui.apply_codex_model_selection()

    assert ui.codex_access_mode == 'approve-for-me'
    assert ui.codex_model_picker_active
    assert ui.codex_model_picker_stage == 'account'
    account = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Account — step 3 of 3' in account
    assert '> Continue' in account
    assert 'Log in' in account
    assert 'Log out' in account

    assert ui.apply_codex_model_selection() is None
    assert not ui.codex_model_picker_active
    selected = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert selected.splitlines()[-1].endswith('F2: GPT-5.3-Codex  --%')


def test_codex_account_picker_returns_login_and_logout_actions():
    ui = TerminalUI(False, lambda _key: None)

    ui.open_codex_model_picker()
    ui.apply_codex_model_selection()
    ui.apply_codex_model_selection()
    ui.move_codex_model_selection(1)
    assert ui.apply_codex_model_selection() == 'login'
    assert not ui.codex_model_picker_active

    ui.open_codex_model_picker()
    ui.apply_codex_model_selection()
    ui.apply_codex_model_selection()
    ui.move_codex_model_selection(2)
    assert ui.apply_codex_model_selection() == 'logout'
    assert not ui.codex_model_picker_active


def test_codex_model_and_access_selection_persist_across_launches(tmp_path):
    settings_path = tmp_path / '.config' / 'rosmon2' / 'agent-settings.json'
    ui = TerminalUI(
        False,
        lambda _key: None,
        agent_settings_path=settings_path,
    )
    ui.set_codex_models([
        {
            'model': 'gpt-5.5',
            'display_name': 'GPT-5.5',
            'is_default': True,
        },
        {
            'model': 'gpt-5.3-codex',
            'display_name': 'GPT-5.3-Codex',
            'is_default': False,
        },
    ])

    ui.open_codex_model_picker()
    ui.move_codex_model_selection(2)
    ui.apply_codex_model_selection()
    ui.move_codex_model_selection(-1)
    ui.apply_codex_model_selection()

    assert settings_path.is_file()
    restored = TerminalUI(
        False,
        lambda _key: None,
        agent_settings_path=settings_path,
    )
    assert restored.codex_selected_model == 'gpt-5.3-codex'
    assert restored.codex_access_mode == 'approve-for-me'


def test_invalid_codex_settings_use_safe_defaults(tmp_path):
    settings_path = tmp_path / 'agent-settings.json'
    settings_path.write_text(
        '{"model": 42, "access_mode": "unknown"}',
        encoding='utf-8',
    )

    ui = TerminalUI(
        False,
        lambda _key: None,
        agent_settings_path=settings_path,
    )

    assert ui.codex_selected_model == 'gpt-5.5'
    assert ui.codex_access_mode == 'full-access'


def test_codex_model_picker_loading_state_keeps_default_available():
    ui = TerminalUI(False, lambda _key: None)
    ui.set_codex_models_loading(True)
    ui.open_codex_model_picker()

    picker = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(80)))

    assert 'loading available models' in picker
    assert 'Codex default' in picker


def test_rosmon_responses_highlight_hardware_names_in_distinct_colors():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_messages.extend([
        ('You', 'is the OAK connected to the UR10e?'),
        (
            'Codex',
            '- Check the UR10e, Robotiq gripper, and OAK-D-Pro connections.',
        ),
    ])

    panel = '\n'.join(ui._codex_panel_lines(140))
    plain = ANSI_RE.sub('', panel)

    assert '\x1b[1;38;2;230;120;255mUR10e' in panel
    assert '\x1b[1;38;2;80;220;255mRobotiq' in panel
    assert '\x1b[1;38;2;100;240;150mOAK-D-Pro' in panel
    assert panel.count('\x1b[1;38;2;100;240;150m') == 1
    assert 'Human: is the OAK connected to the UR10e?' in plain
    assert 'Rosmon: - Check the UR10e, Robotiq gripper, and OAK-D-Pro' in plain
    assert all(ui._visible_len(line) <= 140 for line in panel.splitlines())


def test_diagnosis_agent_summary_is_not_rendered():
    ui = TerminalUI(False, lambda _key: None)
    ui.diagnosis_summary = [
        '- The RealSense D435 and Vive Tracker need attention.',
    ]

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(140)))

    assert 'RealSense' not in panel
    assert 'Vive Tracker' not in panel
    assert 'Agent:' not in panel


def test_diagnosis_mode_renders_selectable_health_table(monkeypatch, capsys):
    ui = TerminalUI(False, lambda _key: None)
    ui.enabled = True
    ui._started = True
    ui.records = [
        ProcessRecord(
            key=0, display_name='ur10e/driver',
            state=State.RUNNING, pid=100,
        ),
        ProcessRecord(
            key=1, display_name='external/camera',
            state=State.CRASHED, return_code=2,
        ),
    ]
    ui.set_diagnosis_rows([
        {
            'record_key': 1, 'selection_key': 'b',
            'name': 'external/camera', 'namespace': 'external',
            'state': 'crashed', 'health': 'Down', 'errors': 7,
            'detail': 'Exit 2; No devices detected',
        },
    ])
    ui.diagnosis_selected = 0
    ui.open_diagnosis()
    monkeypatch.setattr(
        'rosmon2.terminal.shutil.get_terminal_size',
        lambda _fallback: os.terminal_size((160, 30)),
    )
    capsys.readouterr()

    ui.redraw()

    output = ANSI_RE.sub('', capsys.readouterr().out)
    assert 'Diagnosis' in output
    assert 'Node' in output
    assert 'State' in output
    assert 'Errors' in output
    assert 'What might be wrong' in output
    assert ' | Health ' not in output
    # The original node GUI remains above the diagnosis table. The healthy
    # driver appears there once but is omitted from the filtered table.
    assert 'ur10e/driver' in output
    assert output.count('ur10e/driver') == 1
    assert '/external/camera' in output
    assert 'No devices detected' in output
    assert '>b' in output
    assert output.index('ur10e/driver') < output.rindex('Diagnosis')
    assert 'R: restart node' in output
    assert 'K: stop node' in output
    assert 'N: restart namespace' in output
    assert 'X: stop namespace' in output
    assert 'Table  ↑/↓: select  Tab: agent/table mode' in output
    assert 'Focus:' not in output
    assert output.rstrip().splitlines()[-1].endswith('--%')


def test_diagnosis_usage_percentage_is_right_aligned_on_prompt_row():
    ui = TerminalUI(False, lambda _key: None)
    ui.diagnosis_prompt = 'check node b'
    ui.set_codex_usage(73)

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(100)))
    controls = next(
        line for line in panel.splitlines() if 'Table  ↑/↓' in line)
    input_line = panel.splitlines()[-1]

    assert '73%' not in controls
    assert 'R: restart node' in controls
    assert 'K: stop node' in controls
    assert '> check node b' in input_line
    assert 'F11' not in input_line
    assert input_line.endswith('F2: GPT-5.5  73%')
    assert len(input_line) == 100


def test_diagnosis_f2_model_picker_keeps_diagnosis_table_visible():
    ui = TerminalUI(False, lambda _key: None)
    ui.set_diagnosis_rows([{
        'record_key': 0, 'selection_key': 'a',
        'name': 'robot/driver', 'namespace': 'robot',
        'state': 'crashed', 'health': 'Down', 'errors': 1,
        'detail': 'Exited',
    }])
    ui.open_diagnosis()
    ui.open_codex_model_picker()

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(120)))

    assert '/robot/driver' in panel
    assert 'Model' in panel
    assert '> GPT-5.5' in panel
    assert 'Codex default' in panel


def test_diagnosis_control_row_shows_agent_text_focus():
    ui = TerminalUI(False, lambda _key: None)
    ui.diagnosis_chat_focused = True

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(120)))
    controls = next(
        line for line in panel.splitlines() if 'Agent' in line)

    assert 'Tab: agent/table mode' in controls
    assert '↑/↓: select' not in controls
    assert 'Focus:' not in controls


def test_diagnosis_mode_has_separate_streaming_chat():
    ui = TerminalUI(False, lambda _key: None)
    ui.add_codex_message('Codex', 'General Agent answer.')
    ui.add_diagnosis_message('You', 'what is wrong with b?')
    ui.set_diagnosis_chat_running(True)
    ui.begin_diagnosis_stream()
    ui.append_diagnosis_stream(
        '## What might be wrong\n'
        '### Hardware\n- No hardware cause is indicated.\n'
        '### Software\n- Driver stopped.'
    )

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(120)))

    assert 'Human: what is wrong with b?' in panel
    assert 'Rosmon: ## What might be wrong' in panel
    assert 'Rosmon: ### Hardware' in panel
    assert 'Rosmon: ### Software' in panel
    assert 'What to try next' not in panel
    assert 'General Agent answer.' not in panel
    assert 'Thinking…' not in panel
    assert panel.index('Rosmon: ### Software') < panel.index(
        'Table')
    assert panel.splitlines()[-1].startswith(' > ')
    assert panel.splitlines()[-1].endswith('F2: GPT-5.5  --%')


def test_diagnosis_mode_shows_agent_spinner():
    ui = TerminalUI(False, lambda _key: None)
    ui.diagnosis_active = True
    ui.diagnosis_running = True

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(120)))

    assert 'Agent ⠋ checking lifecycle change…' in panel


def test_codex_spinner_advances_while_request_is_running():
    class FakeTimer:
        def __init__(self, callback):
            self.callback = callback
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeLoop:
        def __init__(self):
            self.timers = []

        def call_later(self, _delay, callback):
            timer = FakeTimer(callback)
            self.timers.append(timer)
            return timer

    ui = TerminalUI(False, lambda _key: None)
    ui._loop = FakeLoop()
    ui.codex_active = True

    ui.set_codex_running(True, 'Inspecting…')

    first = ui._loop.timers[-1]
    assert ui.CODEX_SPINNER_FRAMES[ui._codex_spinner_index] == '⠋'
    ui.add_codex_message('You', 'what is wrong with b?')
    running_panel = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Rosmon: ⠋ Analyzing request…' in running_panel
    assert (
        running_panel.index('Human: what is wrong with b?')
        < running_panel.index('Rosmon: ⠋ Analyzing request…')
    )
    first.callback()
    assert ui.CODEX_SPINNER_FRAMES[ui._codex_spinner_index] == '⠙'
    second = ui._loop.timers[-1]
    assert second is not first

    ui.set_codex_running(False, 'Ready')

    assert second.cancelled
    assert ui._codex_spinner_timer is None
    assert ui._codex_spinner_index == 0
    ready_panel = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Ready' not in ready_panel


def test_codex_execution_status_appears_below_human_query_and_animates():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    ui.add_codex_message('You', 'run the node tests')
    ui.set_codex_running(True, 'Inspecting…')
    ui.set_agent_execution(
        'agent', 'Running pytest -q test/test_supervisor.py')

    first = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(120)))

    assert (
        'Rosmon: ⠋ Running pytest -q test/test_supervisor.py…'
        in first
    )
    assert '↳ Running pytest' not in first
    assert first.index('Human: run the node tests') < first.index('Running')
    assert 'Thinking…' not in first

    ui._codex_spinner_index = 1
    second = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(120)))
    assert (
        'Rosmon: ⠙ Running pytest -q test/test_supervisor.py…'
        in second
    )
    assert len(first.splitlines()) == len(second.splitlines())

    ui.set_agent_execution('agent', None)
    cleared = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(120)))
    assert 'Rosmon: ✓ Running pytest -q test/test_supervisor.py' in cleared
    assert 'Rosmon: ⠙ Preparing next step…' in cleared
    assert cleared.index('✓ Running pytest') < cleared.index('Preparing next step')


def test_diagnosis_chat_shows_execution_status_in_agent_text():
    ui = TerminalUI(False, lambda _key: None)
    ui.diagnosis_active = True
    ui.add_diagnosis_message('You', 'inspect the ROS graph')
    ui.set_diagnosis_chat_running(True)
    ui.set_agent_execution('diagnosis', 'Executing ROS operation')

    panel = ANSI_RE.sub('', '\n'.join(ui._diagnosis_panel_lines(120)))

    assert 'Human: inspect the ROS graph' in panel
    assert 'Rosmon: ⠋ Executing ROS operation…' in panel
    assert '↳ Executing ROS operation' not in panel
    assert panel.index('Human: inspect the ROS graph') < panel.index('Executing')
    assert 'Thinking…' not in panel


def test_codex_activity_uses_one_fixed_height_loading_line():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    ui.set_codex_running(True, 'Inspecting…')
    ui.set_agent_execution(
        'agent',
        'Analyzing: Inspecting the live ROS graph and checking the '
        'target-pose controller configuration before selecting an interface',
    )

    activity = ui._agent_activity_lines(
        50, ui.codex_execution_label)
    plain = ANSI_RE.sub('', activity[0])

    assert len(activity) == 1
    assert 'Rosmon: ⠋ Analyzing: Inspecting' in plain
    assert '↳' not in plain
    assert len(plain) <= 50


def test_codex_response_stream_replaces_spinner_and_is_retained():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    ui.add_codex_message('You', 'what is wrong with b?')
    ui.set_codex_running(True, 'Inspecting…')
    ui.begin_codex_stream()

    waiting = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Rosmon: ✓ Analyzing request' in waiting
    assert 'Rosmon: ⠋ Preparing next step…' in waiting

    ui.append_codex_stream('## What might be wrong\n- The ')
    first_chunk = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Rosmon: ## What might be wrong' in first_chunk
    assert 'Rosmon: - The' in first_chunk
    assert 'Analyzing request…' not in first_chunk

    ui.append_codex_stream('driver stopped.')
    second_chunk = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Rosmon: - The driver stopped.' in second_chunk

    ui.finish_codex_stream(
        'Codex', '## What might be wrong\n- The driver stopped.')
    ui.set_codex_running(False, 'Ready')
    finished = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert finished.count('Rosmon: ## What might be wrong') == 1
    assert 'Rosmon: ✓ Analyzing request' in finished
    assert ui.codex_stream_text == ''


def test_reasoning_summary_updates_one_live_step_then_remains_completed():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    ui.set_codex_running(True, 'Inspecting…')

    ui.set_agent_execution('agent', 'Analyzing: Inspecting ROS')
    ui.set_agent_execution(
        'agent', 'Analyzing: Inspecting ROS and controller configuration')

    assert not any(
        message == '✓ Analyzing: Inspecting ROS'
        for _speaker, message in ui.codex_messages
    )
    ui.set_agent_execution('agent', None)
    ui.set_agent_execution('agent', 'Running controller tests')

    panel = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))
    assert 'Rosmon: ✓ Analyzing request' in panel
    assert (
        'Rosmon: ✓ Analyzing: Inspecting ROS and controller configuration'
        in panel
    )
    assert panel.index('✓ Analyzing:') < panel.index('Running controller tests')


def test_repeated_agent_activities_are_retained_within_current_turn():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    ui.add_codex_message('You', 'move the robot tcp 10 mm +X')
    for label in (
            'Analyzing request',
            'Analyzing request',
            'Executing ROS operation',
            'Executing ROS operation',
            'Using rosmon2/rosmon2_start',
            'Using rosmon2/rosmon2_start'):
        ui.set_agent_execution('agent', label)
        ui.set_agent_execution('agent', None)

    assert list(ui.codex_messages).count(
        ('Activity', '✓ Analyzing request')) == 2
    assert list(ui.codex_messages).count(
        ('Activity', '✓ Executing ROS operation')) == 2
    assert list(ui.codex_messages).count(
        ('Activity', '✓ Using rosmon2/rosmon2_start')) == 2


def test_agent_activity_history_keeps_separate_human_turns():
    ui = TerminalUI(False, lambda _key: None)
    ui.add_codex_message('You', 'first request')
    ui.set_agent_execution('agent', 'Executing ROS operation')
    ui.set_agent_execution('agent', None)
    ui.add_codex_message('You', 'second request')
    ui.set_agent_execution('agent', 'Executing ROS operation')
    ui.set_agent_execution('agent', None)

    assert list(ui.codex_messages).count(
        ('Activity', '✓ Executing ROS operation')) == 2


def test_codex_conversation_is_retained_and_scrollable_without_ellipsis():
    ui = TerminalUI(False, lambda _key: None)
    for index in range(24):
        ui.add_codex_message('Codex', f'answer line {index}')

    latest = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))

    assert 'answer line 23' in latest
    assert 'answer line 0' not in latest
    assert 'Viewing' not in latest
    assert ('Codex', '…') not in ui.codex_messages

    ui.scroll_codex(8)
    older = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))

    assert 'answer line 0' in older
    assert 'answer line 23' not in older
    assert 'Viewing' not in older


def test_agent_output_is_unbounded_without_preallocating_blank_rows():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    empty = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))

    for index in range(250):
        ui.add_codex_message('Codex', f'answer line {index}')

    filled = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))

    assert ui.codex_messages[0] == ('Codex', 'answer line 0')
    assert len(ui.codex_messages) == 250
    assert len(empty.splitlines()) == 1
    assert not any(line.strip() == '' for line in empty.splitlines())
    assert 'answer line 249' in filled


def test_agent_scroll_position_is_preserved_while_response_streams():
    ui = TerminalUI(False, lambda _key: None)
    ui.codex_active = True
    for index in range(40):
        ui.add_codex_message('Codex', f'answer line {index}')
    ui._codex_panel_lines(100)
    ui.scroll_codex(5)
    before = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))

    ui.set_codex_running(True, 'Inspecting…')
    ui.append_codex_stream(
        '\n'.join(f'streaming line {index}' for index in range(10)))
    after = ANSI_RE.sub('', '\n'.join(ui._codex_panel_lines(100)))

    assert ui.codex_scroll_offset == 15
    assert 'answer line 34' in before
    assert 'answer line 34' in after
    assert 'streaming line' not in after


def test_codex_long_lines_wrap_to_panel_width():
    ui = TerminalUI(False, lambda _key: None)
    ui.add_codex_message('Codex', 'word ' * 40)

    lines = ui._codex_panel_lines(40)

    assert ui._codex_rendered_line_count > 1
    assert all(ui._visible_len(line) <= 40 for line in lines)


def test_input_reader_decodes_common_f4_sequences(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    pressed = []
    chunks = iter((b'\x1bOS', b'\x1b[14~', b'\x1b[[D'))
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr('rosmon2.terminal.os.read', lambda _fd, _size: next(chunks))
    ui = TerminalUI(False, pressed.append)

    ui._read_input()
    ui._read_input()
    ui._read_input()

    assert pressed == ['F4', 'F4', 'F4']


def test_input_reader_decodes_common_f3_sequences(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    pressed = []
    chunks = iter((b'\x1bOR', b'\x1b[13~', b'\x1b[[C'))
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr(
        'rosmon2.terminal.os.read', lambda _fd, _size: next(chunks))
    ui = TerminalUI(False, pressed.append)

    ui._read_input()
    ui._read_input()
    ui._read_input()

    assert pressed == ['F3', 'F3', 'F3']


def test_input_reader_decodes_common_f2_sequences(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    pressed = []
    chunks = iter((b'\x1bOQ', b'\x1b[12~', b'\x1b[[B'))
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr(
        'rosmon2.terminal.os.read', lambda _fd, _size: next(chunks))
    ui = TerminalUI(False, pressed.append)

    ui._read_input()
    ui._read_input()
    ui._read_input()

    assert pressed == ['F2', 'F2', 'F2']


def test_input_reader_ignores_former_f11_sequence(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    pressed = []
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr(
        'rosmon2.terminal.os.read', lambda _fd, _size: b'\x1b[23~')
    ui = TerminalUI(False, pressed.append)

    ui._read_input()

    assert pressed == []


def test_input_reader_decodes_codex_page_scroll_keys(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    pressed = []
    chunks = iter((b'\x1b[5~', b'\x1b[6~'))
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr('rosmon2.terminal.os.read', lambda _fd, _size: next(chunks))
    ui = TerminalUI(False, pressed.append)

    ui._read_input()
    ui._read_input()

    assert pressed == ['PAGE_UP', 'PAGE_DOWN']


def test_input_reader_decodes_search_navigation_keys(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    pressed = []
    chunks = iter((b'\x1b[A', b'\x1b'))
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr('rosmon2.terminal.os.read', lambda _fd, _size: next(chunks))
    ui = TerminalUI(False, pressed.append)

    ui._read_input()
    ui._read_input()

    assert pressed == ['UP', 'ESC']


def test_input_reader_waits_for_split_escape_sequence(monkeypatch):
    class FakeStdin:
        @staticmethod
        def fileno():
            return 10

    class FakeTimer:
        def __init__(self, callback):
            self.callback = callback
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeLoop:
        def __init__(self):
            self.timers = []

        def call_later(self, _delay, callback):
            timer = FakeTimer(callback)
            self.timers.append(timer)
            return timer

    pressed = []
    chunks = iter((b'\x1b', b'[A'))
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', FakeStdin())
    monkeypatch.setattr('rosmon2.terminal.os.read', lambda _fd, _size: next(chunks))
    ui = TerminalUI(False, pressed.append)
    ui._loop = FakeLoop()

    ui._read_input()
    assert pressed == []
    ui._read_input()

    assert ui._loop.timers[0].cancelled
    assert pressed == ['UP']


def test_start_keeps_shared_terminal_output_blocking(monkeypatch):
    class FakeStream:
        def __init__(self, fd):
            self.fd = fd
            self.output = ''

        @staticmethod
        def isatty():
            return True

        def fileno(self):
            return self.fd

        def write(self, text):
            self.output += text

        @staticmethod
        def flush():
            pass

    class FakeLoop:
        def __init__(self):
            self.readers = []

        def add_reader(self, fd, callback):
            self.readers.append((fd, callback))

    stdin = FakeStream(10)
    stdout = FakeStream(11)
    blocking_calls = []
    monkeypatch.setattr('rosmon2.terminal.sys.stdin', stdin)
    monkeypatch.setattr('rosmon2.terminal.sys.stdout', stdout)
    monkeypatch.setattr('rosmon2.terminal.termios.tcgetattr', lambda _fd: [])
    monkeypatch.setattr('rosmon2.terminal.tty.setcbreak', lambda _fd: None)
    monkeypatch.setattr(
        'rosmon2.terminal.os.set_blocking',
        lambda fd, enabled: blocking_calls.append((fd, enabled)),
    )
    loop = FakeLoop()

    ui = TerminalUI(True, lambda _key: None)
    ui.start(loop)

    assert blocking_calls == [(stdout.fd, True)]
    assert loop.readers == [(stdin.fd, ui._read_input)]
