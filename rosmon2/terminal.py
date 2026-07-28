"""Small ANSI terminal UI patterned after rosmon's interface."""

import json
from math import cos, pi, sin
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import termios
import textwrap
import time
import tty
from collections import deque
from typing import Callable, Iterable, Optional

from .model import ProcessRecord, selection_key, State


ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
SEVERITY_RE = re.compile(r'\[(DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]')
ROS_CONSOLE_PREFIX_RE = re.compile(
    r'^\s*\[(?:DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]'
    r'(?:\s+\[[^\]\r\n]*\])*\s+\[(?P<context>[^\]\r\n]*)\]'
    r'\s*:\s*(?P<message>.*)$'
)
HARDWARE_HIGHLIGHTS = (
    (
        re.compile(
            r'(?<![A-Za-z0-9])(?:UR(?:3|5|10|16|20|30)e?|'
            r'Universal\s+Robots?)(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;230;120;255m',
    ),
    (
        re.compile(
            r'(?<![A-Za-z0-9])Robotiq(?:\s+(?:2F-\d+|Hand-E|FT\s*300))?'
            r'(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;80;220;255m',
    ),
    (
        re.compile(
            r'(?<![A-Za-z0-9])OAK(?:-D(?:-(?:Lite|Pro|S2|PoE))?)?'
            r'(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;100;240;150m',
    ),
    (
        re.compile(
            r'(?<![A-Za-z0-9])(?:Intel\s+)?RealSense(?:\s+[A-Z]?\d{3})?'
            r'(?![A-Za-z0-9])|'
            r'(?<![A-Za-z0-9])(?:D4(?:15|35|55)|L515)(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;255;220;90m',
    ),
    (
        re.compile(
            r'(?<![A-Za-z0-9])(?:LiDAR|Velodyne|Hokuyo|Livox|Ouster)'
            r'(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;255;155;70m',
    ),
    (
        re.compile(
            r'(?<![A-Za-z0-9])ZED(?:\s*(?:2|2i|X|Mini))?'
            r'(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;100;170;255m',
    ),
    (
        re.compile(
            r'(?<![A-Za-z0-9])Vive(?:\s+Tracker)?(?![A-Za-z0-9])',
            re.IGNORECASE,
        ),
        '\x1b[1;38;2;255;120;190m',
    ),
)
RGB_MATRIX = (
    (3.2406, -1.5372, -0.4986),
    (-0.9689, 1.8758, 0.0415),
    (0.0557, -0.2040, 1.0570),
)


def _hsluv_label_color(hue: float):
    """Return rosmon's HSLuv(H, 100, 20) process-label color."""
    lightness = 20.0
    hue_radians = hue / 360.0 * 2.0 * pi
    sin_hue = sin(hue_radians)
    cos_hue = cos(hue_radians)
    sub1 = (lightness + 16.0) ** 3 / 1560896.0
    sub2 = sub1 if sub1 > 0.008856 else lightness / 903.3
    max_chroma = float('inf')
    for m1, m2, m3 in RGB_MATRIX:
        top = (0.99915 * m1 + 1.05122 * m2 + 1.14460 * m3) * sub2
        right = 0.86330 * m3 - 0.17266 * m2
        left = 0.12949 * m3 - 0.38848 * m1
        bottom = (right * sin_hue + left * cos_hue) * sub2
        for boundary in (0.0, 1.0):
            chroma = lightness * (top - 1.05122 * boundary)
            chroma /= bottom + 0.17266 * sin_hue * boundary
            if 0.0 < chroma < max_chroma:
                max_chroma = chroma

    u_value = cos_hue * max_chroma
    v_value = sin_hue * max_chroma
    y_value = ((lightness + 16.0) / 116.0) ** 3
    var_u = u_value / (13.0 * lightness) + 0.19784
    var_v = v_value / (13.0 * lightness) + 0.46834
    x_value = -(9.0 * y_value * var_u)
    x_value /= (var_u - 4.0) * var_v - var_u * var_v
    z_value = (9.0 * y_value - 15.0 * var_v * y_value - var_v * x_value)
    z_value /= 3.0 * var_v

    def from_linear(component):
        if component <= 0.0031308:
            return 12.92 * component
        return 1.055 * component ** (1.0 / 2.4) - 0.055

    xyz = (x_value, y_value, z_value)
    rgb = [from_linear(sum(row[i] * xyz[i] for i in range(3)))
           for row in RGB_MATRIX]
    return tuple(max(0, min(255, int(component * 255.0))) for component in rgb)


class TerminalUI:
    """Render streaming logs with a persistent rosmon-style status bar."""

    OUTPUT_FLUSH_INTERVAL = 1.0 / 60.0
    OUTPUT_BUFFER_LIMIT = 64 * 1024
    REDRAW_INTERVAL = 1.0 / 30.0
    RESIZE_REDRAW_DELAY = 0.10
    CODEX_SPINNER_INTERVAL = 0.12
    CODEX_SPINNER_FRAMES = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')
    CODEX_VISIBLE_LINES = 16
    CODEX_MODEL_VISIBLE_ROWS = 6
    DEFAULT_CODEX_MODEL = 'gpt-5.5'
    DEFAULT_CODEX_MODEL_LABEL = 'GPT-5.5'
    DEFAULT_CODEX_ACCESS_MODE = 'full-access'
    DIAGNOSIS_CHAT_VISIBLE_LINES = 12
    DIAGNOSIS_VISIBLE_ROWS = 10
    DIAGNOSIS_SUMMARY_LINES = 4
    RESET = '\x1b[0m'
    # Exact true-color styles from rosmon's UI. Its packed 0xBBGGRR values
    # 0x404000, 0x606000, and 0xC8C8C8 become the RGB values below.
    BAR = '\x1b[48;2;0;64;64m\x1b[38;2;255;255;255m'
    BAR_KEY = '\x1b[48;2;0;96;96m\x1b[38;2;255;255;255m'
    RUNNING = '\x1b[38;2;0;0;0m\x1b[48;2;24;178;24m'
    CRASHED = '\x1b[38;2;0;0;0m\x1b[48;2;178;24;24m'
    PARTIAL = '\x1b[38;2;0;0;0m\x1b[48;2;200;200;0m'
    WAITING = '\x1b[38;2;0;0;0m\x1b[48;2;178;104;24m'
    AGENT_CREATED = '\x1b[38;2;0;0;0m\x1b[48;2;255;165;0m'
    IDLE = '\x1b[38;2;255;255;255m\x1b[48;2;0;0;0m'
    NODE_SELECTED = '\x1b[38;2;0;0;0m\x1b[48;2;135;206;250m'
    SEARCH_SELECTED = '\x1b[38;2;0;0;0m\x1b[48;2;0;178;178m'
    KEY = '\x1b[38;2;0;0;0m\x1b[48;2;200;200;200m'
    MUTED_KEY = '\x1b[38;2;255;255;255m\x1b[48;2;165;0;0m'

    def __init__(
            self, enabled: bool, on_key: Callable[[str], None],
            output_enabled: bool = True,
            agent_settings_path: Optional[Path] = None):
        self.enabled = bool(enabled and sys.stdin.isatty() and sys.stdout.isatty())
        self.output_enabled = output_enabled
        self.on_key = on_key
        self.records: Iterable[ProcessRecord] = []
        self.selected: Optional[int] = None
        self.namespace_mode = False
        self.namespace_inspect: Optional[str] = None
        self.search_active = False
        self.search_query = ''
        self.search_selected = 0
        # The Codex panel deliberately lives in the same persistent footer as
        # the node list.  A separate full-screen terminal would take control
        # away from the monitor and make it easy to accidentally stop a live
        # launch while asking a diagnostic question.
        self.codex_active = False
        self.codex_prompt = ''
        self.codex_status = 'Ready'
        self.codex_running = False
        self.codex_usage_remaining: Optional[int] = None
        self.codex_usage_loading = False
        self.codex_models = []
        self.codex_models_loading = False
        self.codex_selected_model: Optional[str] = self.DEFAULT_CODEX_MODEL
        self.codex_model_picker_active = False
        self.codex_model_picker_selected = 0
        self.codex_model_picker_stage = 'model'
        self.codex_access_mode = self.DEFAULT_CODEX_ACCESS_MODE
        self.agent_settings_path = (
            Path(agent_settings_path).expanduser()
            if agent_settings_path is not None else None
        )
        self.codex_messages = deque()
        self.codex_stream_text = ''
        self.codex_execution_label: Optional[str] = None
        self.codex_scroll_offset = 0
        self._codex_rendered_line_count = 0
        self._codex_spinner_index = 0
        self._codex_spinner_timer = None
        self.diagnosis_active = False
        self.diagnosis_selected = 0
        self.diagnosis_chat_focused = False
        self.diagnosis_rows = []
        self.diagnosis_running = False
        self.diagnosis_summary = []
        self.diagnosis_prompt = ''
        self.diagnosis_messages = deque()
        self.diagnosis_stream_text = ''
        self.diagnosis_execution_label: Optional[str] = None
        self.diagnosis_chat_running = False
        self.diagnosis_chat_scroll_offset = 0
        self._diagnosis_chat_rendered_line_count = 0
        self.warn_only = False
        self._saved_termios = None
        self._status_lines = 0
        self._buffer = ''
        self._loop = None
        self._escape_timer = None
        self._output_timer = None
        self._output_buffer = []
        self._output_buffer_size = 0
        self._redraw_timer = None
        self._resize_timer = None
        self._resize_handler_registered = False
        self._last_redraw_at = 0.0
        self._render_cache_key = None
        self._render_cache_lines = None
        self._label_names = None
        self._label_width = 8
        self._label_colors = {}
        self._plain_labels = {}
        self._styled_labels = {}
        self._started = False
        self._load_agent_settings()

    def _load_agent_settings(self) -> None:
        """Restore the last confirmed F2 model and access selections."""
        if self.agent_settings_path is None:
            return
        try:
            data = json.loads(
                self.agent_settings_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        model = data.get('model')
        if model is None or (
                isinstance(model, str) and 0 < len(model) <= 256):
            self.codex_selected_model = model
        access_mode = data.get('access_mode')
        valid_access_modes = {
            choice['mode'] for choice in self._codex_access_choices()
        }
        if access_mode in valid_access_modes:
            self.codex_access_mode = access_mode

    def _save_agent_settings(self) -> None:
        """Persist confirmed F2 selections for later mon2 launches."""
        if self.agent_settings_path is None:
            return
        try:
            self.agent_settings_path.parent.mkdir(
                parents=True, exist_ok=True)
            temporary = self.agent_settings_path.with_name(
                self.agent_settings_path.name + '.tmp')
            temporary.write_text(
                json.dumps({
                    'version': 1,
                    'model': self.codex_selected_model,
                    'access_mode': self.codex_access_mode,
                }, indent=2) + '\n',
                encoding='utf-8',
            )
            temporary.replace(self.agent_settings_path)
        except OSError as exc:
            self.codex_messages.append((
                'Codex',
                f'- Could not save Agent settings: {exc}',
            ))

    def open_codex(self) -> None:
        """Show the embedded Codex prompt without disturbing node selection."""
        self.codex_active = True
        self.codex_prompt = ''
        self.codex_model_picker_active = False
        self.codex_model_picker_stage = 'model'
        if not self.codex_running:
            self.codex_status = 'Ready — ask about the selected node'
        self.redraw()

    def close_codex(self) -> None:
        """Hide the Codex prompt while preserving its short conversation history."""
        self.codex_active = False
        self.codex_prompt = ''
        self.codex_model_picker_active = False
        self.codex_model_picker_stage = 'model'
        self.redraw()

    def open_diagnosis(self) -> None:
        """Show the live node-health table."""
        self.diagnosis_active = True
        self.diagnosis_prompt = ''
        self.diagnosis_chat_focused = False
        self.codex_model_picker_active = False
        self.codex_model_picker_stage = 'model'
        self.diagnosis_selected = min(
            self.diagnosis_selected, max(0, len(self.diagnosis_rows) - 1))
        self.redraw()

    def close_diagnosis(self) -> None:
        """Hide the live node-health table."""
        self.diagnosis_active = False
        self.diagnosis_chat_focused = False
        self.codex_model_picker_active = False
        self.codex_model_picker_stage = 'model'
        self.redraw()

    def set_diagnosis_rows(self, rows) -> None:
        """Replace the diagnosis table with a fresh health snapshot."""
        self.diagnosis_rows = list(rows)
        self.diagnosis_selected = min(
            self.diagnosis_selected, max(0, len(self.diagnosis_rows) - 1))
        self.redraw()

    def set_diagnosis_running(self, running: bool) -> None:
        """Update the diagnosis agent activity indicator."""
        self.diagnosis_running = running
        if running:
            self._start_codex_spinner()
        elif not self.codex_running and not self.diagnosis_chat_running:
            self._stop_codex_spinner()
        self.redraw()

    def set_diagnosis_chat_running(self, running: bool) -> None:
        """Update activity for a Human-started Diagnosis conversation turn."""
        if (
                running
                and not self.diagnosis_chat_running
                and self.diagnosis_execution_label is None):
            self.diagnosis_execution_label = 'Analyzing request'
        self.diagnosis_chat_running = running
        if running:
            self._start_codex_spinner()
        else:
            self._complete_agent_execution('diagnosis')
            if not self.codex_running and not self.diagnosis_running:
                self._stop_codex_spinner()
        self.redraw()

    def set_diagnosis_summary(self, text: str) -> None:
        """Store the hidden agent result for diagnostics and tests."""
        clean = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', '')
        self.diagnosis_summary = [
            line.strip() for line in clean.splitlines() if line.strip()
        ][-self.DIAGNOSIS_SUMMARY_LINES:]

    def set_codex_running(self, running: bool, status: str) -> None:
        """Update the small live status indicator in the Codex panel."""
        if (
                running
                and not self.codex_running
                and self.codex_execution_label is None):
            self.codex_execution_label = 'Analyzing request'
        self.codex_running = running
        self.codex_status = status
        if running:
            self._start_codex_spinner()
        else:
            self._complete_agent_execution('agent')
            if not self.diagnosis_running and not self.diagnosis_chat_running:
                self._stop_codex_spinner()
        self.redraw()

    def _complete_agent_execution(self, mode: str) -> None:
        """Move a finished activity into the retained conversation timeline."""
        if mode == 'diagnosis':
            label = self.diagnosis_execution_label
            messages = self.diagnosis_messages
            self.diagnosis_execution_label = None
        else:
            label = self.codex_execution_label
            messages = self.codex_messages
            self.codex_execution_label = None
        if label is not None:
            messages.append(('Activity', f'✓ {label}'))

    def set_agent_execution(
            self, mode: str, label: Optional[str]) -> None:
        """Advance the retained Agent activity timeline."""
        if label is not None:
            label = ' '.join(
                label.replace('\x1b', '').replace('\x00', '').split())
            label = label[:240].strip() or None
        previous = (
            self.diagnosis_execution_label
            if mode == 'diagnosis' else
            self.codex_execution_label
        )
        updating_reasoning_summary = (
            previous is not None
            and label is not None
            and previous.startswith('Analyzing:')
            and label.startswith('Analyzing:')
        )
        if (
                previous is not None
                and previous != label
                and not updating_reasoning_summary):
            self._complete_agent_execution(mode)
        if mode == 'diagnosis':
            self.diagnosis_execution_label = label
        else:
            self.codex_execution_label = label
        if label is not None:
            self._start_codex_spinner()
        self.redraw()

    def set_codex_usage(
            self, remaining_percent: Optional[int], *, loading: bool = False
            ) -> None:
        """Update the authenticated Codex rate-limit percentage."""
        self.codex_usage_remaining = remaining_percent
        self.codex_usage_loading = loading
        self.redraw()

    def set_codex_models(self, models, *, loading: bool = False) -> None:
        """Store the visible models advertised by the installed Codex CLI."""
        cleaned = []
        seen = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            model = item.get('model')
            if not isinstance(model, str) or not model or model in seen:
                continue
            seen.add(model)
            display_name = item.get('display_name')
            cleaned.append({
                'model': model,
                'display_name': (
                    display_name
                    if isinstance(display_name, str) and display_name
                    else model
                ),
                'is_default': bool(item.get('is_default')),
            })
        self.codex_models = cleaned
        self.codex_models_loading = loading
        available = {item['model'] for item in cleaned}
        if self.codex_selected_model not in available:
            self.codex_selected_model = None
        self.codex_model_picker_selected = self._codex_model_choice_index(
            self.codex_selected_model)
        self.redraw()

    def set_codex_models_loading(self, loading: bool) -> None:
        self.codex_models_loading = loading
        self.redraw()

    def _codex_model_choices(self):
        choices = [{
            'model': None,
            'display_name': 'Codex default',
            'is_default': True,
        }] + self.codex_models
        available = {item['model'] for item in choices}
        if (
                self.codex_selected_model is not None
                and self.codex_selected_model not in available):
            choices.append({
                'model': self.codex_selected_model,
                'display_name': self.codex_model_label(),
                'is_default': False,
            })
        return choices

    def _codex_model_choice_index(self, model: Optional[str]) -> int:
        for index, choice in enumerate(self._codex_model_choices()):
            if choice['model'] == model:
                return index
        return 0

    def codex_model_label(self) -> str:
        """Return a short label for the model used by new Agent turns."""
        if self.codex_selected_model is None:
            return 'Default'
        if self.codex_selected_model == self.DEFAULT_CODEX_MODEL:
            return self.DEFAULT_CODEX_MODEL_LABEL
        for item in self.codex_models:
            if item['model'] == self.codex_selected_model:
                return item['display_name']
        return self.codex_selected_model

    @staticmethod
    def _codex_access_choices():
        return [
            {
                'mode': 'approve-for-me',
                'display_name': 'Approve for me',
                'description': 'Codex auto-reviews applicable approvals',
            },
            {
                'mode': 'full-access',
                'display_name': 'Full access',
                'description': 'No sandbox or approval prompts',
            },
        ]

    @staticmethod
    def _codex_account_choices():
        return [
            {
                'action': 'continue',
                'display_name': 'Continue',
                'description': 'Keep the current Codex login',
            },
            {
                'action': 'login',
                'display_name': 'Log in',
                'description': 'Sign in with Codex device authentication',
            },
            {
                'action': 'logout',
                'display_name': 'Log out',
                'description': 'Remove the stored Codex login',
            },
        ]

    def _codex_access_choice_index(self) -> int:
        for index, choice in enumerate(self._codex_access_choices()):
            if choice['mode'] == self.codex_access_mode:
                return index
        return 0

    def open_codex_model_picker(self) -> None:
        self.codex_model_picker_active = True
        self.codex_model_picker_stage = 'model'
        self.codex_model_picker_selected = self._codex_model_choice_index(
            self.codex_selected_model)
        self.redraw()

    def close_codex_model_picker(self) -> None:
        self.codex_model_picker_active = False
        self.codex_model_picker_stage = 'model'
        self.redraw()

    def move_codex_model_selection(self, amount: int) -> None:
        if self.codex_model_picker_stage == 'access':
            choices = self._codex_access_choices()
        elif self.codex_model_picker_stage == 'account':
            choices = self._codex_account_choices()
        else:
            choices = self._codex_model_choices()
        if not choices:
            return
        self.codex_model_picker_selected = max(
            0,
            min(
                len(choices) - 1,
                self.codex_model_picker_selected + amount,
            ),
        )
        self.redraw()

    def apply_codex_model_selection(self) -> Optional[str]:
        if self.codex_model_picker_stage == 'model':
            choices = self._codex_model_choices()
            if choices:
                index = min(
                    self.codex_model_picker_selected, len(choices) - 1)
                self.codex_selected_model = choices[index]['model']
            self.codex_model_picker_stage = 'access'
            self.codex_model_picker_selected = (
                self._codex_access_choice_index())
        elif self.codex_model_picker_stage == 'access':
            choices = self._codex_access_choices()
            if choices:
                index = min(
                    self.codex_model_picker_selected, len(choices) - 1)
                self.codex_access_mode = choices[index]['mode']
            self._save_agent_settings()
            self.codex_model_picker_stage = 'account'
            self.codex_model_picker_selected = 0
        else:
            choices = self._codex_account_choices()
            index = min(
                self.codex_model_picker_selected, len(choices) - 1)
            action = choices[index]['action']
            self.codex_model_picker_active = False
            self.codex_model_picker_stage = 'model'
            self.redraw()
            return None if action == 'continue' else action
        self.redraw()
        return None

    def _start_codex_spinner(self) -> None:
        """Animate Codex activity without blocking launch or log processing."""
        if (self._loop is not None and self._codex_spinner_timer is None
                and (
                    self.codex_running
                    or self.diagnosis_running
                    or self.diagnosis_chat_running
                    or self.codex_execution_label is not None
                    or self.diagnosis_execution_label is not None
                )):
            self._codex_spinner_timer = self._loop.call_later(
                self.CODEX_SPINNER_INTERVAL, self._advance_codex_spinner)

    def _advance_codex_spinner(self) -> None:
        self._codex_spinner_timer = None
        if not (
                self.codex_running
                or self.diagnosis_running
                or self.diagnosis_chat_running
                or self.codex_execution_label is not None
                or self.diagnosis_execution_label is not None):
            return
        self._codex_spinner_index = (
            self._codex_spinner_index + 1
        ) % len(self.CODEX_SPINNER_FRAMES)
        if self.codex_active or self.diagnosis_active:
            self.redraw()
        self._start_codex_spinner()

    def _stop_codex_spinner(self) -> None:
        if self._codex_spinner_timer is not None:
            self._codex_spinner_timer.cancel()
            self._codex_spinner_timer = None
        self._codex_spinner_index = 0

    def add_codex_message(self, speaker: str, text: str) -> None:
        """Append terminal-safe transcript output below the node list."""
        clean = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', '')
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        if not lines:
            return
        for line in lines:
            self.codex_messages.append((speaker, line))
        if speaker in ('You', 'Human'):
            self.codex_scroll_offset = 0
        self.redraw()

    def begin_codex_stream(self) -> None:
        """Prepare one progressively rendered Agent response."""
        self._complete_agent_execution('agent')
        self.codex_stream_text = ''
        self._request_redraw()

    def append_codex_stream(self, text: str) -> None:
        """Append a terminal-safe response chunk and schedule a bounded redraw."""
        clean = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', '')
        clean = ''.join(
            character for character in clean
            if character in ('\n', '\t') or ord(character) >= 32
        )
        if not clean:
            return
        self.codex_stream_text += clean
        self._request_redraw()

    def finish_codex_stream(self, speaker: str, text: str) -> None:
        """Atomically replace the live response with its retained final text."""
        self._complete_agent_execution('agent')
        self.codex_stream_text = ''
        self.add_codex_message(speaker, text)

    def clear_codex_stream(self) -> None:
        """Discard an unfinished response after cancellation or failure."""
        if not self.codex_stream_text:
            return
        self.codex_stream_text = ''
        self._request_redraw()

    def add_diagnosis_message(self, speaker: str, text: str) -> None:
        """Keep a separate terminal-safe Diagnosis conversation."""
        clean = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', '')
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        for line in lines:
            self.diagnosis_messages.append((speaker, line))
        if lines:
            if speaker in ('You', 'Human'):
                self.diagnosis_chat_scroll_offset = 0
            self.redraw()

    def begin_diagnosis_stream(self) -> None:
        self._complete_agent_execution('diagnosis')
        self.diagnosis_stream_text = ''
        self._request_redraw()

    def append_diagnosis_stream(self, text: str) -> None:
        clean = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', '')
        clean = ''.join(
            character for character in clean
            if character in ('\n', '\t') or ord(character) >= 32
        )
        if clean:
            self.diagnosis_stream_text += clean
            self._request_redraw()

    def finish_diagnosis_stream(self, speaker: str, text: str) -> None:
        self._complete_agent_execution('diagnosis')
        self.diagnosis_stream_text = ''
        self.add_diagnosis_message(speaker, text)

    def clear_diagnosis_stream(self) -> None:
        if self.diagnosis_stream_text:
            self.diagnosis_stream_text = ''
            self._request_redraw()

    def scroll_diagnosis_chat(self, amount: int) -> None:
        maximum = max(
            0,
            self._diagnosis_chat_rendered_line_count
            - self.DIAGNOSIS_CHAT_VISIBLE_LINES,
        )
        self.diagnosis_chat_scroll_offset = max(
            0, min(maximum, self.diagnosis_chat_scroll_offset + amount))
        self.redraw()

    def scroll_codex(self, amount: int) -> None:
        """Move through retained Codex transcript lines without terminal scrollback."""
        maximum = max(
            0, self._codex_rendered_line_count - self.CODEX_VISIBLE_LINES)
        self.codex_scroll_offset = max(
            0, min(maximum, self.codex_scroll_offset + amount))
        self.redraw()

    def start(self, loop) -> None:
        """Enter raw input mode and register the keyboard reader."""
        if self._started:
            return
        self._loop = loop
        if not self.enabled:
            return
        self._saved_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        # stdin/stdout/stderr commonly share one open file description on a
        # pseudo-terminal.  Making stdin nonblocking therefore also makes
        # launch's output writes nonblocking, which can fail with EAGAIN and
        # shut down the complete LaunchService.  add_reader() only invokes
        # _read_input when data is ready, so blocking terminal I/O is safe.
        os.set_blocking(sys.stdout.fileno(), True)
        loop.add_reader(sys.stdin.fileno(), self._read_input)
        sys.stdout.write('\x1b[?25l')
        sys.stdout.flush()
        self._started = True
        add_signal_handler = getattr(loop, 'add_signal_handler', None)
        if add_signal_handler is not None:
            try:
                add_signal_handler(signal.SIGWINCH, self._schedule_resize_redraw)
                self._resize_handler_registered = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    def close(self, loop=None) -> None:
        """Restore the user's terminal even when launch was interrupted."""
        self._stop_codex_spinner()
        if self._output_timer is not None:
            self._output_timer.cancel()
            self._output_timer = None
        self._flush_output(redraw=False)
        if not self._started:
            self._loop = None
            return
        if loop is not None:
            try:
                loop.remove_reader(sys.stdin.fileno())
            except Exception:
                pass
        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None
        if self._redraw_timer is not None:
            self._redraw_timer.cancel()
            self._redraw_timer = None
        if self._resize_timer is not None:
            self._resize_timer.cancel()
            self._resize_timer = None
        resize_loop = loop if loop is not None else self._loop
        if self._resize_handler_registered and resize_loop is not None:
            try:
                resize_loop.remove_signal_handler(signal.SIGWINCH)
            except (AttributeError, NotImplementedError, RuntimeError, ValueError):
                pass
            self._resize_handler_registered = False
        self._erase_status()
        sys.stdout.write(self.RESET + '\x1b[?25h')
        sys.stdout.flush()
        if self._saved_termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_termios)
        self._started = False
        self._loop = None

    def set_records(self, records: Iterable[ProcessRecord]) -> None:
        self.records = records
        names = tuple(record.display_name for record in records)
        if names != self._label_names:
            self._rebuild_label_cache(names)
        self.redraw()

    @staticmethod
    def namespace_for(record: ProcessRecord) -> str:
        """Return the top-level namespace containing a process."""
        parts = [part for part in record.display_name.strip('/').split('/') if part]
        return parts[0] if len(parts) > 1 else '/'

    def namespaces(self):
        """Return stable namespace groups represented by the current records."""
        values = {self.namespace_for(record) for record in self.records}
        return sorted(values, key=lambda value: (value != '/', value))

    def records_in_namespace(self, namespace: str):
        """Return every process recursively grouped under a top-level namespace."""
        return [record for record in self.records
                if self.namespace_for(record) == namespace]

    def visible_records(self):
        """Return nodes visible in normal or namespace inspection mode."""
        if self.namespace_mode and self.namespace_inspect is not None:
            return self.records_in_namespace(self.namespace_inspect)
        return list(self.records)

    def search_matches(self):
        """Return visible nodes whose full names contain the search query."""
        return [record for record in self.visible_records()
                if self.search_query in record.display_name]

    @staticmethod
    def namespace_counts(records):
        """Return running and non-running process counts for a namespace."""
        alive = sum(record.state is State.RUNNING for record in records)
        return alive, len(records) - alive

    @classmethod
    def namespace_style(cls, records):
        """Color a namespace green, yellow, or red from its live/dead counts."""
        alive, dead = cls.namespace_counts(records)
        if dead == 0:
            return cls.RUNNING
        if alive == 0:
            return cls.CRASHED
        return cls.PARTIAL

    @classmethod
    def state_style(cls, state: State):
        """Return the status color for one process state."""
        return {
            State.RUNNING: cls.RUNNING,
            State.CRASHED: cls.CRASHED,
            State.WAITING: cls.WAITING,
            State.IDLE: cls.IDLE,
        }[state]

    @classmethod
    def record_style(cls, record: ProcessRecord):
        """Use orange only for a running Agent-created node."""
        if record.agent_created and record.state == State.RUNNING:
            return cls.AGENT_CREATED
        return cls.state_style(record.state)

    def log(self, source: str, text: str, is_stderr: bool = False,
            severity: Optional[str] = None) -> None:
        """Print one or more process output lines above the status bar."""
        if not self.output_enabled:
            return
        self._ensure_label_cache()
        width = max(self._label_width, len(source))
        clean = text.replace('\r\n', '\n').replace('\r', '\n')
        output = []
        for line in clean.splitlines():
            line_severity = self._severity(line, severity, is_stderr)
            if self.warn_only and line_severity not in ('WARNING', 'ERROR', 'FATAL'):
                continue
            line = self._message_body(line)
            label = self._plain_label(source, width)
            if self.enabled:
                label = self._styled_label(source, width)
                style = {
                    'DEBUG': '\x1b[32m',
                    'WARNING': '\x1b[33m',
                    'ERROR': '\x1b[31m',
                    'FATAL': '\x1b[1;31m',
                }.get(line_severity, '')
                if style:
                    line = style + line + self.RESET
            output.append(f'{label} {line}\n')
        if output:
            self._queue_output(output)

    def notice(self, text: str, error: bool = False) -> None:
        self.log('rosmon2', text, severity='ERROR' if error else 'INFO')

    def flush(self) -> None:
        """Immediately write pending process output."""
        if self._output_timer is not None:
            self._output_timer.cancel()
            self._output_timer = None
        had_output = bool(self._output_buffer)
        self._flush_output()
        if not had_output:
            sys.stdout.flush()

    @staticmethod
    def _severity(line: str, explicit: Optional[str], is_stderr: bool) -> str:
        """Determine severity without assuming all ROS stderr output is an error."""
        match = SEVERITY_RE.search(ANSI_RE.sub('', line))
        value = match.group(1) if match else explicit
        if value == 'WARN':
            value = 'WARNING'
        if value:
            return value.upper()
        return 'ERROR' if is_stderr else 'INFO'

    @staticmethod
    def _message_body(line: str) -> str:
        """Keep rosmon's function/logger field while removing severity and time."""
        plain = ANSI_RE.sub('', line)
        match = ROS_CONSOLE_PREFIX_RE.match(plain)
        if match is None:
            return line
        return f'[{match.group("context")}]: {match.group("message")}'

    def _label_color(self, source: str):
        """Return the cached color for a process label."""
        self._ensure_label_cache()
        return self._label_colors.get(source)

    def _ensure_label_cache(self) -> None:
        if self._label_names is None:
            names = tuple(record.display_name for record in self.records)
            self._rebuild_label_cache(names)

    def _rebuild_label_cache(self, names) -> None:
        """Cache widths and colors that only change with the process list."""
        self._label_names = names
        self._label_width = max([len(name) for name in names] + [8])
        self._label_colors = {}
        process_count = max(1, len(names))
        for index, name in enumerate(names):
            if name not in self._label_colors:
                hue = index * 360.0 / process_count
                self._label_colors[name] = _hsluv_label_color(hue)
        self._plain_labels = {}
        self._styled_labels = {}

    def _plain_label(self, source: str, width: int) -> str:
        key = (source, width)
        cached = self._plain_labels.get(key)
        if cached is None:
            cached = f'{source:>{width}}:'
            self._plain_labels[key] = cached
        return cached

    def _styled_label(self, source: str, width: int) -> str:
        key = (source, width)
        cached = self._styled_labels.get(key)
        if cached is not None:
            return cached
        label = self._plain_label(source, width)
        background = self._label_colors.get(source)
        if background is None:
            styled = '\x1b[38;2;178;178;178m' + label + self.RESET
        else:
            red, green, blue = background
            styled = (
                f'\x1b[48;2;{red};{green};{blue}m'
                '\x1b[38;2;255;255;255m' + label + self.RESET
            )
        self._styled_labels[key] = styled
        return styled

    def _queue_output(self, output) -> None:
        self._output_buffer.extend(output)
        self._output_buffer_size += sum(len(item) for item in output)
        if self._loop is None or self._output_buffer_size >= self.OUTPUT_BUFFER_LIMIT:
            if self._output_timer is not None:
                self._output_timer.cancel()
                self._output_timer = None
            self._flush_output()
        elif self._output_timer is None:
            self._output_timer = self._loop.call_later(
                self.OUTPUT_FLUSH_INTERVAL, self._run_output_flush)

    def _run_output_flush(self) -> None:
        self._output_timer = None
        self._flush_output()

    def _flush_output(self, *, redraw: bool = True) -> None:
        if not self._output_buffer:
            return
        output = ''.join(self._output_buffer)
        self._output_buffer.clear()
        self._output_buffer_size = 0
        if (redraw and self.enabled and self._started
                and self._render_cache_lines is not None):
            # Keep the status bar in the same terminal write as the new logs.
            # Writing the erase sequence, logs, and status separately makes
            # the status area visibly flash while output is streaming.
            if self._redraw_timer is not None:
                self._redraw_timer.cancel()
                self._redraw_timer = None
            erase = self._take_status_erase()
            lines = self._render_cache_lines
            sys.stdout.write(erase + output + self._status_text(lines))
            self._status_lines = len(lines)
            self._last_redraw_at = time.monotonic()
            sys.stdout.flush()
            return

        self._erase_status()
        sys.stdout.write(output)
        redrawn = self._request_redraw() if redraw else False
        if not redrawn:
            sys.stdout.flush()

    def redraw(self, *, prefix: str = '') -> None:
        if self._redraw_timer is not None:
            self._redraw_timer.cancel()
            self._redraw_timer = None
        if not self.enabled or not self._started:
            return
        # Keep erasing the previous status and drawing its replacement in one
        # terminal write.  Selection keys redraw immediately; sending ESC[J
        # first can otherwise leave a visible blank frame in some terminals.
        erase = prefix + self._take_status_erase()
        # Never write into the terminal's final column.  VTE-based terminals
        # such as Terminator mark that cell for an automatic wrap; the
        # following newline can then make one logical footer row occupy two
        # physical rows and invalidate our cursor-up count after a resize.
        columns = max(4, shutil.get_terminal_size((100, 24)).columns - 1)
        render_key = self._status_render_key(columns)
        if self._render_cache_key == render_key:
            self._draw_status_lines(self._render_cache_lines, prefix=erase)
            return
        sep = '\x1b[38;2;0;64;64m' + ('▂' * columns) + self.RESET
        showing_namespaces = self.namespace_mode and self.namespace_inspect is None
        if self.search_active:
            menu = self.BAR + f' Searching for: {self.search_query}'
        elif self.selected is None:
            menu = self._menu_item(
                'A-Z', 'Namespace actions' if showing_namespaces else 'Node select')
            menu += self._menu_item('F3', 'Diagnosis')
            menu += self._menu_item('F4', 'Agent')
            menu += self._menu_item(
                'F5', 'Node mode' if self.namespace_mode else 'Namespace view')
            if self.namespace_mode and self.namespace_inspect is not None:
                menu += self._menu_item('Backspace', 'Namespaces')
            menu += self._menu_item('F6', 'Start all')
            menu += self._menu_item('F7', 'Stop all')
            menu += self._menu_item('F8', 'Toggle Warn+')
            menu += self._menu_item('F9', 'Mute all')
            menu += self._menu_item('F10', 'Unmute all')
            menu += self._menu_item('/', 'Node search')
            if self.warn_only:
                menu += ' \x1b[30;45m ! WARN+ output only ! ' + self.RESET
            if any(r.muted for r in self.records):
                menu += ' \x1b[30;43m ! Caution: Nodes muted ! ' + self.RESET
        else:
            if showing_namespaces:
                namespaces = self.namespaces()
                if self.selected >= len(namespaces):
                    self.selected = None
                    return self.redraw()
                namespace = namespaces[self.selected]
                count = len(self.records_in_namespace(namespace))
                menu = self.BAR + f" Namespace '{namespace}' has {count} node(s). Actions:"
                menu += self._menu_item('s', 'start all')
                menu += self._menu_item('k', 'stop all')
                menu += self._menu_item('i', 'inspect')
                menu += self._menu_item('m', 'mute namespace')
                menu += self._menu_item('u', 'unmute namespace')
            else:
                records = self.visible_records()
                if self.selected >= len(records):
                    self.selected = None
                    return self.redraw()
                record = records[self.selected]
                menu = self.BAR + f" Node '{record.display_name}' is {record.state.value}. Actions:"
                menu += self._menu_item('F4', 'ask Agent')
                menu += self._menu_item('s', 'start')
                menu += self._menu_item('k', 'stop')
                menu += self._menu_item('d', 'debug')
                menu += self._menu_item('u' if record.muted else 'm',
                                        'unmute' if record.muted else 'mute')
        menu = self._fit(menu, columns)

        if self.search_active:
            entries = [(record.display_name, self.record_style(record), record.muted)
                       for record in self.search_matches()]
        elif showing_namespaces:
            entries = []
            for namespace in self.namespaces():
                members = self.records_in_namespace(namespace)
                alive, dead = self.namespace_counts(members)
                entries.append((
                    f'{namespace} [{alive}:{dead}]',
                    self.namespace_style(members),
                    bool(members) and all(record.muted for record in members),
                ))
        else:
            entries = [(record.display_name, self.record_style(record), record.muted)
                       for record in self.visible_records()]

        blocks = []
        line = ''
        for index, (display_name, state_style, muted) in enumerate(entries):
            if self.search_active:
                name = display_name.lstrip('/')
                max_name_length = max(1, columns - 2)
                if len(name) > max_name_length:
                    name = name[:max_name_length - 1] + '…'
                label = f' {name} '
                style = (
                    self.SEARCH_SELECTED
                    if self.search_selected == index else state_style
                )
                block = style + label + self.RESET
                plain_len = len(label)
                if self._visible_len(line) + plain_len + 1 > columns and line:
                    blocks.append(line)
                    line = block
                else:
                    line += (' ' if line else '') + block
                continue

            key = selection_key(index)
            key_text = key if key is not None else ' '
            key_style = self.MUTED_KEY if muted else self.KEY
            name = display_name if showing_namespaces else display_name.lstrip('/')
            # Keep the complete process name whenever it fits.  The status
            # area already wraps blocks onto additional rows, so shortening
            # every name to 13 characters only hides useful node identity.
            max_name_length = max(1, columns - 3)
            if len(name) > max_name_length:
                name = name[:max_name_length - 1] + '…'
            selected = self.selected == index
            label = f'[{name}]' if selected and not showing_namespaces else f' {name} '
            label_style = (
                self.NODE_SELECTED
                if selected and not showing_namespaces else state_style
            )
            block = key_style + key_text + label_style + label + self.RESET
            plain_len = 1 + len(label)
            if self._visible_len(line) + plain_len + 1 > columns and line:
                blocks.append(line)
                line = block
            else:
                line += (' ' if line else '') + block
        if line:
            blocks.append(line)
        if not blocks:
            message = ' no matching nodes ' if self.search_active else ' waiting for processes '
            blocks = [self.IDLE + message + self.RESET]

        lines = [sep, menu] + blocks
        if self.codex_active:
            lines.extend(self._codex_panel_lines(columns))
        if self.diagnosis_active:
            lines.extend(self._diagnosis_panel_lines(columns))
        self._render_cache_key = render_key
        self._render_cache_lines = tuple(lines)
        self._draw_status_lines(lines, prefix=erase)

    def _request_redraw(self) -> bool:
        """Coalesce log-driven status updates to avoid redrawing per message."""
        if not self.enabled or not self._started:
            return False
        if self._loop is None:
            self.redraw()
            return True
        elapsed = time.monotonic() - self._last_redraw_at
        delay = self.REDRAW_INTERVAL - elapsed
        if delay <= 0:
            self.redraw()
            return True
        elif self._redraw_timer is None:
            self._redraw_timer = self._loop.call_later(
                delay, self._run_scheduled_redraw)
        return False

    def _run_scheduled_redraw(self) -> None:
        self._redraw_timer = None
        self.redraw()

    def _schedule_resize_redraw(self) -> None:
        """Recover the footer after terminal reflow settles."""
        if self._loop is None or not self._started:
            return
        if self._resize_timer is not None:
            self._resize_timer.cancel()
        self._resize_timer = self._loop.call_later(
            self.RESIZE_REDRAW_DELAY, self._redraw_after_resize)

    def _redraw_after_resize(self) -> None:
        self._resize_timer = None
        if not self.enabled or not self._started:
            return
        # Once resize events settle, erase and rebuild only the footer at its
        # existing origin.  The log area and terminal scrollback stay intact.
        self.redraw()

    def _status_render_key(self, columns: int):
        records = tuple(
            (
                record.display_name,
                record.state,
                record.muted,
                record.agent_created,
            )
            for record in self.records
        )
        return (
            columns,
            records,
            self.selected,
            self.namespace_mode,
            self.namespace_inspect,
            self.search_active,
            self.search_query,
            self.search_selected,
            self.warn_only,
            self.codex_active,
            self.codex_prompt,
            self.codex_status,
            self.codex_running,
            self.codex_usage_remaining,
            self.codex_usage_loading,
            tuple(
                (
                    item['model'],
                    item['display_name'],
                    item['is_default'],
                )
                for item in self.codex_models
            ),
            self.codex_models_loading,
            self.codex_selected_model,
            self.codex_model_picker_active,
            self.codex_model_picker_selected,
            self.codex_model_picker_stage,
            self.codex_access_mode,
            self._codex_spinner_index,
            self.codex_scroll_offset,
            tuple(self.codex_messages),
            self.codex_stream_text,
            self.codex_execution_label,
            self.diagnosis_active,
            self.diagnosis_selected,
            self.diagnosis_chat_focused,
            tuple(
                tuple(sorted(row.items())) for row in self.diagnosis_rows
            ),
            self.diagnosis_running,
            self.diagnosis_prompt,
            tuple(self.diagnosis_messages),
            self.diagnosis_stream_text,
            self.diagnosis_execution_label,
            self.diagnosis_chat_running,
            self.diagnosis_chat_scroll_offset,
        )

    @staticmethod
    def _diagnosis_cell(value, width: int) -> str:
        """Fit one plain table cell without breaking the table columns."""
        text = str(value)
        if len(text) > width:
            text = text[:max(1, width - 1)] + '…'
        return text.ljust(width)

    @classmethod
    def _highlight_hardware_names(
            cls, text: str, restore_style: Optional[str] = None) -> str:
        """Give recognized hardware families distinct, stable colors."""
        restore = restore_style or cls.BAR
        for pattern, color in HARDWARE_HIGHLIGHTS:
            text = pattern.sub(
                lambda match, style=color: (
                    style + match.group(0) + cls.RESET + restore
                ),
                text,
            )
        return text

    def _diagnosis_panel_lines(self, columns: int):
        """Render only the selectable nodes that currently need attention."""
        rows = self.diagnosis_rows
        selected = min(self.diagnosis_selected, max(0, len(rows) - 1))
        self.diagnosis_selected = selected
        visible_count = min(self.DIAGNOSIS_VISIBLE_ROWS, len(rows))
        start = max(0, selected - visible_count + 1)
        start = min(start, max(0, len(rows) - visible_count))
        visible_rows = rows[start:start + visible_count]

        # Reserve useful widths for both the full node identity and diagnosis.
        fixed = 30
        flexible = max(20, columns - fixed)
        node_width = max(10, min(34, flexible * 3 // 5))
        detail_width = max(10, flexible - node_width)
        header = (
            f" Key | {self._diagnosis_cell('Node', node_width)} | "
            f"{self._diagnosis_cell('State', 8)} | "
            f"{self._diagnosis_cell('Errors', 6)} | "
            f"{self._diagnosis_cell('What might be wrong', detail_width)}"
        )
        divider = (
            '-----+' + ('-' * (node_width + 2)) + '+'
            + ('-' * 10) + '+' + ('-' * 8) + '+'
            + ('-' * (detail_width + 2))
        )
        title = self.BAR + ' Diagnosis'
        if self.diagnosis_running:
            spinner = self.CODEX_SPINNER_FRAMES[self._codex_spinner_index]
            title += f' — Agent {spinner} checking lifecycle change…'
        elif rows:
            title += f' — {len(rows)} node(s) need attention'

        lines = [self._fit(title + self.RESET, columns)]
        lines.append(self._fit(self.BAR + header + self.RESET, columns))
        lines.append(self._fit(self.BAR + divider + self.RESET, columns))
        if not visible_rows:
            message = (
                ' - All nodes are healthy.'
                if self.records else
                ' - Waiting for processes.'
            )
            lines.append(self._fit(
                self.BAR + message + self.RESET, columns))
        for offset, row in enumerate(visible_rows):
            index = start + offset
            marker = '>' if index == selected else ' '
            key = row.get('selection_key', ' ')
            text = (
                f"{marker}{key}  | "
                f"{self._diagnosis_cell('/' + row['name'].lstrip('/'), node_width)} | "
                f"{self._diagnosis_cell(row['state'], 8)} | "
                f"{self._diagnosis_cell(row['errors'], 6)} | "
                f"{self._diagnosis_cell(row['detail'], detail_width)}"
            )
            style = (
                self.NODE_SELECTED
                if index == selected and not self.diagnosis_chat_focused
                else self.BAR
            )
            lines.append(self._fit(style + text + self.RESET, columns))
        if self.codex_model_picker_active:
            lines.extend(self._codex_model_picker_lines(columns))
            return lines
        transcript = self._chat_transcript_lines(
            self.diagnosis_messages,
            self.diagnosis_stream_text,
            columns,
        )
        previous_count = self._diagnosis_chat_rendered_line_count
        if (
                self.diagnosis_chat_scroll_offset > 0
                and len(transcript) > previous_count):
            self.diagnosis_chat_scroll_offset += (
                len(transcript) - previous_count)
        self._diagnosis_chat_rendered_line_count = len(transcript)
        maximum = max(
            0, len(transcript) - self.DIAGNOSIS_CHAT_VISIBLE_LINES)
        self.diagnosis_chat_scroll_offset = min(
            self.diagnosis_chat_scroll_offset, maximum)
        end = len(transcript) - self.diagnosis_chat_scroll_offset
        chat_start = max(0, end - self.DIAGNOSIS_CHAT_VISIBLE_LINES)
        visible_chat = transcript[chat_start:end]
        for text in visible_chat:
            lines.append(self._fit(self.BAR + text + self.RESET, columns))
        if (
                self.diagnosis_execution_label is not None
                or (
                    self.diagnosis_chat_running
                    and not self.diagnosis_stream_text.strip()
                )):
            lines.extend(self._agent_activity_lines(
                columns, self.diagnosis_execution_label))
        if self.diagnosis_chat_focused:
            focus_controls = 'Agent  Tab: agent/table mode'
        else:
            focus_controls = (
                'Table  ↑/↓: select  Tab: agent/table mode')
        controls = (
            f' {focus_controls}  R: restart node  K: stop node  '
            'N: restart namespace  X: stop namespace  F3/Esc: close '
        )
        lines.append(self._fit(self.BAR + controls + self.RESET, columns))
        usage = (
            '--%'
            if self.codex_usage_loading or self.codex_usage_remaining is None
            else f'{self.codex_usage_remaining}%'
        )
        prompt = f' > {self.diagnosis_prompt}'
        prompt += '█' if not self.diagnosis_chat_running else ''
        model = f'F2: {self.codex_model_label()}'
        maximum_model_width = max(
            0, columns - len(usage) - 6)
        if len(model) > maximum_model_width:
            model = (
                model[:max(1, maximum_model_width - 1)] + '…'
                if maximum_model_width else ''
            )
        right = f'{model}  {usage}' if model else usage
        prompt_width = max(0, columns - len(right) - 1)
        prompt = prompt[:prompt_width]
        spacing = max(1, columns - len(prompt) - len(right))
        input_line = self.BAR + prompt + (' ' * spacing) + right + self.RESET
        lines.append(self._fit(input_line, columns))
        return lines

    def _chat_transcript_lines(self, messages, stream_text: str, columns: int):
        """Wrap one Human/Rosmon transcript for either interactive mode."""
        transcript = []

        def append_message(speaker: str, message: str) -> None:
            prefix = ' Human: ' if speaker == 'You' else ' Rosmon: '
            available = max(10, columns - len(prefix))
            wrapped = textwrap.wrap(
                message,
                width=available,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=False,
            ) or ['']
            for index, part in enumerate(wrapped):
                line_prefix = prefix if index == 0 else ' ' * len(prefix)
                if speaker != 'You':
                    part = self._highlight_hardware_names(part)
                transcript.append(line_prefix + part)

        for speaker, message in messages:
            append_message(speaker, message)
        if stream_text.strip():
            stream_lines = [
                line.strip()
                for line in stream_text.splitlines()
                if line.strip()
            ]
            for message in stream_lines or [stream_text]:
                append_message('Codex', message)
        return transcript

    def _agent_activity_lines(
            self, columns: int, label: Optional[str]):
        """Render current activity on one stable-height spinner row."""
        spinner = self.CODEX_SPINNER_FRAMES[self._codex_spinner_index]
        activity = label or 'Preparing next step'
        return [self._fit(
            self.BAR
            + f' Rosmon: {spinner} {activity}… '
            + self.RESET,
            columns,
        )]

    def _codex_panel_lines(self, columns: int):
        """Render the compact, focused Codex conversation below the nodes."""
        if self.codex_model_picker_active:
            return self._codex_model_picker_lines(columns)
        transcript = self._chat_transcript_lines(
            self.codex_messages,
            self.codex_stream_text,
            columns,
        )
        previous_count = self._codex_rendered_line_count
        if (
                self.codex_scroll_offset > 0
                and len(transcript) > previous_count):
            self.codex_scroll_offset += len(transcript) - previous_count
        self._codex_rendered_line_count = len(transcript)
        maximum = max(0, len(transcript) - self.CODEX_VISIBLE_LINES)
        self.codex_scroll_offset = min(self.codex_scroll_offset, maximum)
        end = len(transcript) - self.codex_scroll_offset
        start = max(0, end - self.CODEX_VISIBLE_LINES)
        lines = []
        visible_transcript = transcript[start:end]
        for text in visible_transcript:
            lines.append(self._fit(self.BAR + text + self.RESET, columns))
        if (
                self.codex_execution_label is not None
                or (self.codex_running and not self.codex_stream_text.strip())):
            lines.extend(self._agent_activity_lines(
                columns, self.codex_execution_label))
        usage = (
            '--%'
            if self.codex_usage_loading or self.codex_usage_remaining is None
            else f'{self.codex_usage_remaining}%'
        )
        prompt = f' > {self.codex_prompt}'
        prompt += '█' if not self.codex_running else ''
        model = f'F2: {self.codex_model_label()}'
        maximum_model_width = max(
            0, columns - len(usage) - 6)
        if len(model) > maximum_model_width:
            model = (
                model[:max(1, maximum_model_width - 1)] + '…'
                if maximum_model_width else ''
            )
        right = f'{model}  {usage}' if model else usage
        prompt_width = max(0, columns - len(right) - 1)
        prompt = prompt[:prompt_width]
        spacing = max(1, columns - len(prompt) - len(right))
        input_line = self.BAR + prompt + (' ' * spacing) + right + self.RESET
        lines.append(self._fit(input_line, columns))
        return lines

    def _codex_model_picker_lines(self, columns: int):
        """Render the model, access, and account selector."""
        access_stage = self.codex_model_picker_stage == 'access'
        account_stage = self.codex_model_picker_stage == 'account'
        if access_stage:
            choices = self._codex_access_choices()
        elif account_stage:
            choices = self._codex_account_choices()
        else:
            choices = self._codex_model_choices()
        selected = min(
            self.codex_model_picker_selected, max(0, len(choices) - 1))
        self.codex_model_picker_selected = selected
        visible_count = min(self.CODEX_MODEL_VISIBLE_ROWS, len(choices))
        start = max(0, selected - visible_count + 1)
        start = min(start, max(0, len(choices) - visible_count))
        visible = choices[start:start + visible_count]
        if access_stage:
            title = ' Access — step 2 of 3'
        elif account_stage:
            title = ' Account — step 3 of 3'
        else:
            title = ' Model — step 1 of 3'
        if self.codex_models_loading and not access_stage and not account_stage:
            title += ' — loading available models…'
        lines = [self._fit(self.BAR + title + self.RESET, columns)]
        for offset, choice in enumerate(visible):
            index = start + offset
            marker = '>' if index == selected else ' '
            suffix = ''
            if access_stage:
                if choice['mode'] == self.codex_access_mode:
                    suffix = ' (current)'
                text = (
                    f' {marker} {choice["display_name"]} — '
                    f'{choice["description"]}{suffix}'
                )
            elif account_stage:
                text = (
                    f' {marker} {choice["display_name"]} — '
                    f'{choice["description"]}'
                )
            else:
                if choice['model'] is not None and choice['is_default']:
                    suffix = ' (CLI default)'
                text = f' {marker} {choice["display_name"]}{suffix}'
            style = self.NODE_SELECTED if index == selected else self.BAR
            lines.append(self._fit(style + text + self.RESET, columns))
        controls = (
            ' ↑/↓: select  Enter: choose  Esc: close'
            if account_stage else
            ' ↑/↓: select  Enter: next  Esc: close'
        )
        lines.append(self._fit(self.BAR + controls + self.RESET, columns))
        return lines

    def _draw_status_lines(self, lines, *, prefix: str = '') -> None:
        sys.stdout.write(prefix + self._status_text(lines))
        sys.stdout.flush()
        self._status_lines = len(lines)
        self._last_redraw_at = time.monotonic()

    @staticmethod
    def _status_text(lines) -> str:
        return '\n'.join(lines) + '\n' + f'\x1b[{len(lines)}A\r'

    def _menu_item(self, key: str, label: str) -> str:
        return f'{self.BAR_KEY} {key}:{self.BAR} {label} {self.RESET}'

    def _erase_status(self) -> None:
        erase = self._take_status_erase()
        if erase:
            sys.stdout.write(erase)

    def _take_status_erase(self) -> str:
        if self.enabled and self._status_lines:
            self._status_lines = 0
            return '\r\x1b[J'
        return ''

    def _read_input(self) -> None:
        try:
            data = os.read(sys.stdin.fileno(), 64).decode(errors='ignore')
        except (BlockingIOError, OSError):
            return
        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None
        self._buffer += data
        # Consume the former F11 shortcut without inserting its escape
        # sequence into the active prompt.
        self._buffer = self._buffer.replace('\x1b[23~', '')
        keys = {
            '\x1bOQ': 'F2', '\x1b[12~': 'F2', '\x1b[[B': 'F2',
            '\x1bOR': 'F3', '\x1b[13~': 'F3', '\x1b[[C': 'F3',
            '\x1bOS': 'F4', '\x1b[14~': 'F4', '\x1b[[D': 'F4',
            '\x1b[15~': 'F5', '\x1b[17~': 'F6', '\x1b[18~': 'F7', '\x1b[19~': 'F8',
            '\x1b[20~': 'F9', '\x1b[21~': 'F10',
            '\x1b[5~': 'PAGE_UP', '\x1b[6~': 'PAGE_DOWN',
            '\x1b[A': 'UP', '\x1b[B': 'DOWN',
            '\x1b[C': 'RIGHT', '\x1b[D': 'LEFT',
        }
        while self._buffer:
            matched = False
            for sequence, name in keys.items():
                if self._buffer.startswith(sequence):
                    self._buffer = self._buffer[len(sequence):]
                    self.on_key(name)
                    matched = True
                    break
            if matched:
                continue
            if self._buffer == '\x1b':
                if self._loop is None:
                    self._flush_escape()
                    continue
                self._escape_timer = self._loop.call_later(0.03, self._flush_escape)
                break
            if self._buffer.startswith('\x1b') and len(self._buffer) < 3:
                break
            char, self._buffer = self._buffer[0], self._buffer[1:]
            self.on_key(char)

    def _flush_escape(self) -> None:
        """Emit a standalone Escape after allowing time for an arrow sequence."""
        self._escape_timer = None
        if self._buffer == '\x1b':
            self._buffer = ''
            self.on_key('ESC')

    @staticmethod
    def _visible_len(text: str) -> int:
        return len(ANSI_RE.sub('', text))

    @staticmethod
    def _truncate_ansi(text: str, columns: int) -> str:
        """Truncate visible text while retaining embedded ANSI styles."""
        output = []
        position = 0
        visible = 0
        for match in ANSI_RE.finditer(text):
            chunk = text[position:match.start()]
            remaining = columns - visible
            if remaining <= 0:
                break
            output.append(chunk[:remaining])
            visible += min(len(chunk), remaining)
            if len(chunk) > remaining:
                break
            output.append(match.group())
            position = match.end()
        else:
            remaining = columns - visible
            if remaining > 0:
                output.append(text[position:position + remaining])
        return ''.join(output)

    def _fit(self, text: str, columns: int) -> str:
        plain = ANSI_RE.sub('', text)
        if len(plain) <= columns:
            return text + self.BAR + (' ' * (columns - len(plain))) + self.RESET
        # The status remains useful on narrow terminals even without every action.
        return self._truncate_ansi(text, columns) + self.RESET
