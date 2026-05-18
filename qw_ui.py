"""
qw_ui.py - User Interface Layer

Contains:
- Dash app initialization and configuration
- All layout components (tabs, tables, charts, modals)
- All callback functions for interactivity
- CSS styling and JavaScript injection
- Flask routes for task actions and static files
- Event handlers for buttons, dropdowns, and inputs
"""

import os, json, time, threading, uuid
from datetime import datetime, timedelta, timezone
import dash
from dash import dcc, html, Input, Output, State, MATCH, ALL, no_update, ctx, clientside_callback
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from flask import send_file, request, jsonify

# Import logic layer
from qw_logic import (
    task_manager, verification_manager, optimizer_manager,
    parse_signal_text, sanitize_for_json, _parse_timestamp,
    fetch_symbols, clear_parquet_cache,
    cached_small_stats_data, stats_cache_version
)

# Import database layer
from qw_database import LOGS_DIR, MARKET_DATA_DIR, get_database_info

# =============================================================================
# Dash App Initialization
# =============================================================================

app = dash.Dash(__name__, suppress_callback_exceptions=True, prevent_initial_callbacks='initial_duplicate')

# =============================================================================
# Flask Routes
# =============================================================================

@app.server.route('/task-action', methods=['POST'])
def task_action():
    """Handle task actions (stop/pause/save) via Flask route."""
    data = request.get_json()
    task_id = data.get('task_id')
    action = data.get('action')
    task = task_manager.get_task(task_id)
    
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    
    if action == 'stop':
        if task.status == "running" and task_manager.stop_task(task_id):
            task.add_log("Stop signal sent.")
            return jsonify({'success': True})
    elif action == 'pause':
        task_manager.pause_task(task_id)
        new_label = "Resume" if getattr(task, 'paused', False) else "Pause"
        return jsonify({'success': True, 'new_label': new_label})
    elif action == 'save':
        fname = os.path.join(LOGS_DIR, f"task_{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        with open(fname, "w") as f:
            f.write("\n".join(task.logs))
        task.add_log(f"Log saved to {fname}")
        return jsonify({'success': True})
    
    return jsonify({'error': 'Invalid action'}), 400


@app.server.route('/task-card/<task_id>')
def serve_task_card(task_id):
    """Serve HTML for individual task cards."""
    task = task_manager.get_task(task_id)
    if not task:
        return "Task not found", 404
    
    summary = f"Symbols: {', '.join(task.symbols)} | TF: {task.timeframe}"
    if task.mode == 'period' and task.start_date:
        summary += f" | {task.start_date.date()} to {task.end_date.date()}"
    
    # Generate task card HTML (simplified version)
    html_content = f"""
    <div class="task-card" id="task-{task_id}">
        <h4>Task {task_id[:8]}...</h4>
        <p>{summary}</p>
        <p>Status: {task.status}</p>
        <p>Progress: {task.progress:.1f}%</p>
        <div class="task-buttons">
            <button data-action="pause" data-task-id="{task_id}">Pause</button>
            <button data-action="stop" data-task-id="{task_id}">Stop</button>
            <button data-action="save" data-task-id="{task_id}">Save Log</button>
        </div>
    </div>
    """
    return html_content


# =============================================================================
# App Layout Configuration
# =============================================================================

app.index_string = '''
<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<style>
/* Highlight column light yellow – applied directly to th/td cells */
.highlight-column {
    background-color: #fff9c4 !important;
}
/* Highlight row light green – applied to tr, affects its td children */
.highlight-row td {
    background-color: #c8e6c9 !important;
}
/* Hide column – use visibility:collapse to keep table layout stable */
.hidden-column td,
.hidden-column th {
    visibility: collapse !important;
    background-color: inherit !important;
}
/* For the hidden header, show a narrow marker using pseudo-element */
.hidden-column th {
    visibility: visible !important;
    width: 20px !important;
    min-width: 20px !important;
    max-width: 20px !important;
    padding: 2px 0 !important;
    text-align: center !important;
    color: transparent !important;
    font-size: 0 !important;
    position: relative;
    background-color: #f0f0f0 !important;
}
.hidden-column th::before {
    content: "⋮";
    position: absolute;
    left: 0;
    right: 0;
    text-align: center;
    color: black;
    font-size: 14px;
    font-weight: bold;
}
/* Keep thead background sticky */
th {
    background-color: #f0f0f0;
    position: sticky;
    top: 0;
}
/* Strike-through for cells where level was never reached */
.strike-through {
    text-decoration: line-through !important;
}
</style>
</head>
<body>
{%app_entry%}
<footer>
{%config%}
{%scripts%}
{%renderer%}
<script>
// Global store for hidden columns (by zero-based column index)
let hiddenColumns = new Set();

// Function to apply hidden column classes to the current table
function applyHiddenColumns() {
    const container = document.querySelector('#task-table-container');
    if (!container) return;
    const table = container.querySelector('table');
    if (!table) return;
    hiddenColumns.forEach(colIndex => {
        const columnCells = table.querySelectorAll(`tr th:nth-child(${colIndex+1}), tr td:nth-child(${colIndex+1})`);
        columnCells.forEach(cell => {
            cell.classList.add('hidden-column');
            cell.classList.remove('highlight-column');
        });
    });
}

// Button click handler with immediate feedback
document.addEventListener('click', function(e) {
    let target = e.target;
    if ((target.tagName === 'BUTTON' || (target.tagName === 'DIV' && target.classList.contains('interactive-button'))) && target.id) {
        try {
            let actionType = target.getAttribute('data-action');
            let taskId = target.getAttribute('data-task-id');

            // Fallback to old JSON parsing method for backward compatibility
            if (!actionType || !taskId) {
                console.warn('Using legacy JSON ID parsing.');
                let idObj = JSON.parse(target.id);
                if (idObj.type === 'pause-task' || idObj.type === 'stop-task' || idObj.type === 'save-log') {
                    taskId = idObj.index;
                    actionType = idObj.type === 'save-log' ? 'save' : (idObj.type === 'stop-task' ? 'stop' : 'pause');
                }
            }

            if (actionType && taskId) {
                if (actionType === 'stop' || actionType === 'pause') {
                    fetch('/task-action', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({task_id: taskId, action: actionType})
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success && actionType === 'pause') {
                            target.innerText = data.new_label;
                        }
                    })
                    .catch(err => {
                        console.error('Task action fetch failed:', err);
                    });
                }
                else {
                    if (actionType === 'chart') {
                        window.dash_clientside.set_props('chart-button-trigger', { data: { task_id: taskId, action: actionType } });
                    } else if (actionType === 'details') {
                        window.dash_clientside.set_props('strategy-details-trigger', { data: { task_id: taskId } });
                    } else if (actionType === 'impulse') {
                        window.dash_clientside.set_props('impulse-button-trigger', { data: { task_id: taskId, action: actionType } });
                    }
                }
            }
        } catch (e) {
            console.error('Button click handler error:', e, 'Target ID:', target.id);
        }
    }
});

// Toggle column highlight on header click
document.addEventListener('click', function(e) {
    if (e.target.closest('button')) return;
    let cell = e.target.closest('th, td');
    if (!cell) return;
    let table = cell.closest('table');
    if (!table) return;
    
    if (cell.tagName === 'TH') {
        let colIndex = cell.cellIndex;
        let columnCells = table.querySelectorAll(`tr th:nth-child(${colIndex+1}), tr td:nth-child(${colIndex+1})`);
        let isHighlighted = columnCells.length > 0 && columnCells[0].classList.contains('highlight-column');
        columnCells.forEach(c => {
            if (isHighlighted) c.classList.remove('highlight-column');
            else c.classList.add('highlight-column');
        });
    }
    else if (cell.tagName === 'TD') {
        let row = cell.parentNode;
        if (row.classList.contains('highlight-row')) {
            row.classList.remove('highlight-row');
        } else {
            row.classList.add('highlight-row');
        }
    }
});

// Toggle column visibility on double-click of header
document.addEventListener('dblclick', function(e) {
    let th = e.target.closest('th');
    if (!th) return;
    let table = th.closest('table');
    if (!table) return;
    let colIndex = th.cellIndex;
    let columnCells = table.querySelectorAll(`tr th:nth-child(${colIndex+1}), tr td:nth-child(${colIndex+1})`);
    if (columnCells.length === 0) return;
    
    let isHidden = columnCells[0].classList.contains('hidden-column');
    if (isHidden) {
        columnCells.forEach(cell => cell.classList.remove('hidden-column'));
        hiddenColumns.delete(colIndex);
    } else {
        columnCells.forEach(cell => {
            cell.classList.add('hidden-column');
            cell.classList.remove('highlight-column');
        });
        hiddenColumns.add(colIndex);
    }
});
</script>
</footer>
</body>
</html>
'''


# =============================================================================
# Layout Components
# =============================================================================

def render_tab(tab):
    """Render content for a specific tab."""
    if tab == 'signals':
        return html.Div([
            html.H2("Signal Management"),
            dcc.Upload(id='upload-signal-file', children=html.Div([
                'Drag and Drop or ', html.A('Select Signal File')
            ])),
            dcc.Textarea(id='paste-signals-text', placeholder='Or paste signals here...', rows=10),
            html.Button('Parse Signals', id='parse-signals-btn', n_clicks=0),
            html.Div(id='signal-parsing-output'),
            html.Div(id='tasks-container')
        ])
    
    elif tab == 'database':
        return html.Div([
            html.H2("Database Management"),
            html.Div(id='database-info'),
            html.Button('Refresh Info', id='refresh-db-btn', n_clicks=0),
            html.Div(id='db-maintenance-controls')
        ])
    
    elif tab == 'verification':
        return html.Div([
            html.H2("Data Verification"),
            html.Button('Start Basic Verification', id='start-verify-btn', n_clicks=0),
            html.Button('Start Deep Verification', id='start-deep-verify-btn', n_clicks=0),
            html.Button('Stop Verification', id='stop-verify-btn', n_clicks=0),
            html.Button('Generate Report', id='generate-report-btn', n_clicks=0),
            html.Pre(id='verification-log')
        ])
    
    elif tab == 'summary':
        return html.Div([
            html.H2("Summary Statistics"),
            html.Div(id='summary-stats')
        ])
    
    return html.Div([html.H3("Tab content not implemented")])


# Main app layout
app.layout = html.Div([
    # Hidden stores for state management
    dcc.Store(id='stored-task-ids', data=[]),
    dcc.Store(id='click-store', data={}),
    dcc.Store(id='chart-button-trigger', data={}),
    dcc.Store(id='strategy-details-trigger', data={}),
    dcc.Store(id='impulse-button-trigger', data={}),
    dcc.Store(id='measure-points', data=[]),
    
    # Tab navigation
    html.H1("Bybit Signal Downloader & Analyzer"),
    dcc.Tabs(id='main-tabs', value='signals', children=[
        dcc.Tab(label='Signals', value='signals'),
        dcc.Tab(label='Database', value='database'),
        dcc.Tab(label='Verification', value='verification'),
        dcc.Tab(label='Summary', value='summary')
    ]),
    
    # Tab content
    html.Div(id='tab-content'),
    
    # Modals
    html.Div(id='chart-modal'),
    html.Div(id='strategy-details-modal'),
    html.Div(id='impulse-details-modal'),
    
    # Status bar
    html.Div(id='status-bar', style={'position': 'fixed', 'bottom': 0, 'width': '100%', 'background': '#f0f0f0', 'padding': '10px'})
])


# =============================================================================
# Callbacks - Signal Processing
# =============================================================================

@app.callback(
    Output('signal-parsing-output', 'children'),
    Input('parse-signals-btn', 'n_clicks'),
    State('upload-signal-file', 'contents'),
    State('upload-signal-file', 'filename'),
    State('paste-signals-text', 'value'),
    prevent_initial_call=True
)
def parse_signal_file(n_clicks, contents, filename, pasted_text):
    """Parse uploaded or pasted signal files."""
    if not n_clicks:
        return no_update
    
    signals = []
    
    # Try uploaded file first
    if contents:
        try:
            content_type, content_string = contents.split(',')
            decoded = content_string.decode('base64') if 'base64' in content_type else content_string
            text = decoded if isinstance(decoded, str) else decoded.decode('utf-8')
            signals = parse_signal_text(text)
        except Exception as e:
            return html.Div(f"Error reading file: {e}", style={'color': 'red'})
    
    # Try pasted text
    elif pasted_text:
        signals = parse_signal_text(pasted_text)
    
    if not signals:
        return html.Div("No valid signals found", style={'color': 'orange'})
    
    return html.Div(f"Successfully parsed {len(signals)} signals", style={'color': 'green'})


@app.callback(
    Output('tasks-container', 'children'),
    Input('parse-signals-btn', 'n_clicks'),
    State('stored-task-ids', 'data'),
    prevent_initial_call=True
)
def update_tasks_display(n_clicks, stored_ids):
    """Update the display of active tasks."""
    if not stored_ids:
        return html.Div("No active tasks")
    
    task_cards = []
    for task_id in stored_ids:
        task = task_manager.get_task(task_id)
        if task:
            task_cards.append(html.Div([
                html.H4(f"Task {task_id[:8]}..."),
                html.P(f"Symbols: {', '.join(task.symbols)}"),
                html.P(f"Status: {task.status}"),
                html.P(f"Progress: {task.progress:.1f}%"),
                html.Button('Pause', **{'data-action': 'pause', 'data-task-id': task_id}),
                html.Button('Stop', **{'data-action': 'stop', 'data-task-id': task_id}),
            ], style={'border': '1px solid #ccc', 'margin': '10px', 'padding': '10px'}))
    
    return task_cards


# =============================================================================
# Callbacks - Tab Navigation
# =============================================================================

@app.callback(
    Output('tab-content', 'children'),
    Input('main-tabs', 'value')
)
def update_tab_content(tab):
    """Render content based on selected tab."""
    return render_tab(tab)


# =============================================================================
# Callbacks - Database Management
# =============================================================================

@app.callback(
    Output('database-info', 'children'),
    Input('refresh-db-btn', 'n_clicks'),
    prevent_initial_call=True
)
def update_database_info(n_clicks):
    """Display database information."""
    try:
        info = get_database_info()
        return html.Pre(json.dumps(info, indent=2, default=str))
    except Exception as e:
        return html.Div(f"Error: {e}", style={'color': 'red'})


# =============================================================================
# Callbacks - Verification
# =============================================================================

@app.callback(
    Output('verification-log', 'children'),
    Input('start-verify-btn', 'n_clicks'),
    Input('start-deep-verify-btn', 'n_clicks'),
    Input('stop-verify-btn', 'n_clicks'),
    Input('generate-report-btn', 'n_clicks'),
    prevent_initial_call=True
)
def control_verification(start_clicks, deep_clicks, stop_clicks, report_clicks):
    """Control verification process."""
    triggered = ctx.triggered_id
    
    if triggered == 'start-verify-btn':
        verification_manager.start_verification(deep=False)
        return "Basic verification started..."
    elif triggered == 'start-deep-verify-btn':
        verification_manager.start_verification(deep=True)
        return "Deep verification started..."
    elif triggered == 'stop-verify-btn':
        verification_manager.stop_verification()
        return "Verification stopped"
    elif triggered == 'generate-report-btn':
        report = verification_manager.generate_integrity_report()
        return json.dumps(report, indent=2, default=str)
    
    # Auto-update logs
    logs = verification_manager.get_logs()
    return "\n".join(logs) if logs else "No logs yet"


# =============================================================================
# Callbacks - Chart Modal
# =============================================================================

@app.callback(
    Output('chart-modal', 'children'),
    Input('chart-button-trigger', 'data'),
    Input('chart-modal-close', 'n_clicks'),
    prevent_initial_call=True
)
def toggle_chart_modal(trigger_data, close_clicks):
    """Show/hide chart modal."""
    triggered = ctx.triggered_id
    
    if triggered == 'chart-modal-close' or not trigger_data:
        return no_update
    
    task_id = trigger_data.get('task_id')
    task = task_manager.get_task(task_id)
    
    if not task:
        return html.Div("Task not found")
    
    # Generate chart (placeholder - actual implementation would call update_task_chart)
    return html.Div([
        html.H3(f"Chart for Task {task_id[:8]}"),
        dcc.Graph(id='task-chart'),
        html.Button('Close', id='chart-modal-close')
    ])


# =============================================================================
# Callbacks - Strategy Details Modal
# =============================================================================

@app.callback(
    Output('strategy-details-modal', 'children'),
    Input('strategy-details-trigger', 'data'),
    Input('strategy-modal-close', 'n_clicks'),
    prevent_initial_call=True
)
def toggle_strategy_details_modal(trigger_data, close_clicks):
    """Show/hide strategy details modal."""
    triggered = ctx.triggered_id
    
    if triggered == 'strategy-modal-close' or not trigger_data:
        return no_update
    
    task_id = trigger_data.get('task_id')
    task = task_manager.get_task(task_id)
    
    if not task:
        return html.Div("Task not found")
    
    signals = task.strategy_signals if hasattr(task, 'strategy_signals') else []
    
    return html.Div([
        html.H3(f"Strategy Signals for Task {task_id[:8]}"),
        html.P(f"Total signals: {len(signals)}"),
        html.Button('Close', id='strategy-modal-close')
    ])


# =============================================================================
# Callbacks - Impulse Details Modal
# =============================================================================

@app.callback(
    Output('impulse-details-modal', 'children'),
    Input('impulse-button-trigger', 'data'),
    Input('impulse-modal-close', 'n_clicks'),
    prevent_initial_call=True
)
def toggle_impulse_modal(trigger_data, close_clicks):
    """Show/hide impulse details modal."""
    triggered = ctx.triggered_id
    
    if triggered == 'impulse-modal-close' or not trigger_data:
        return no_update
    
    task_id = trigger_data.get('task_id')
    task = task_manager.get_task(task_id)
    
    if not task:
        return html.Div("Task not found")
    
    impulses = task.impulse_events if hasattr(task, 'impulse_events') else []
    
    return html.Div([
        html.H3(f"Impulse Events for Task {task_id[:8]}"),
        html.P(f"Total impulses: {len(impulses)}"),
        html.Button('Close', id='impulse-modal-close')
    ])


# =============================================================================
# Callbacks - Summary Statistics
# =============================================================================

@app.callback(
    Output('summary-stats', 'children'),
    Input('main-tabs', 'value'),
    Input('stats-cache-version', 'data'),
    prevent_initial_call=True
)
def update_summary_stats(tab, cache_version):
    """Update summary statistics display."""
    if tab != 'summary':
        return no_update
    
    # Get all tasks
    tasks = task_manager.get_all_tasks()
    
    total_candles = sum(getattr(t, 'candle_count', 0) for t in tasks)
    completed = sum(1 for t in tasks if t.status == 'completed')
    running = sum(1 for t in tasks if t.status == 'running')
    
    return html.Div([
        html.H3("Overall Statistics"),
        html.P(f"Total Tasks: {len(tasks)}"),
        html.P(f"Completed: {completed}"),
        html.P(f"Running: {running}"),
        html.P(f"Total Candles: {total_candles:,}")
    ])


# =============================================================================
# Callbacks - Status Bar
# =============================================================================

@app.callback(
    Output('status-bar', 'children'),
    Input('status-update-timer', 'n_intervals'),
    prevent_initial_call=True
)
def update_status_bar(n_intervals):
    """Update status bar with current system state."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    active_tasks = len([t for t in task_manager.get_all_tasks() if t.status == 'running'])
    
    return f"Status: {active_tasks} active tasks | Last updated: {timestamp}"


# =============================================================================
# Clientside Callbacks
# =============================================================================

# Measurement tool callbacks
clientside_callback(
    """
    function(n_clicks, current) {
        return !current;
    }
    """,
    Output('measure-mode', 'data', allow_duplicate=True),
    Input('toggle-measure-btn', 'n_clicks'),
    State('measure-mode', 'data'),
    prevent_initial_call=True
)

# RSI toggle
clientside_callback(
    """
    function(n_clicks, current) {
        return !current;
    }
    """,
    Output('rsi-visible', 'data'),
    Input('toggle-rsi-btn', 'n_clicks'),
    State('rsi-visible', 'data'),
    prevent_initial_call=True
)

# Strategy toggle
clientside_callback(
    """
    function(n_clicks, current) {
        return !current;
    }
    """,
    Output('strategy-visible', 'data'),
    Input('toggle-strategy-btn', 'n_clicks'),
    State('strategy-visible', 'data'),
    prevent_initial_call=True
)

# Events toggle
clientside_callback(
    """
    function(n_clicks, current) {
        return !current;
    }
    """,
    Output('events-visible', 'data'),
    Input('toggle-events-btn', 'n_clicks'),
    State('events-visible', 'data'),
    prevent_initial_call=True
)

# Impulses toggle
clientside_callback(
    """
    function(n_clicks, current) {
        return !current;
    }
    """,
    Output('impulses-visible', 'data'),
    Input('toggle-impulses-btn', 'n_clicks'),
    State('impulses-visible', 'data'),
    prevent_initial_call=True
)


# =============================================================================
# Helper Functions
# =============================================================================

def format_timestamp(ts):
    """Format timestamp for display."""
    if isinstance(ts, (datetime, pd.Timestamp)):
        return ts.strftime('%Y-%m-%d %H:%M:%S')
    return str(ts)


def format_stat_value(value, total=None):
    """Format statistic value with percentage."""
    if value is None:
        return "N/A"
    if total:
        pct = (value / total * 100) if total > 0 else 0
        return f"{value:,} ({pct:.1f}%)"
    return f"{value:,}"


def get_adverse_range(pct):
    """Calculate adverse movement range based on percentage."""
    return pct * 0.5 if pct else 0


# =============================================================================
# Run the App
# =============================================================================




# ======================================================================
# MISSING CALLBACKS FROM ORIGINAL FILE
# ======================================================================

@app.callback(
    Output("task-ids-store", "data", allow_duplicate=True),
    Input({"type": "remove-task", "index": ALL}, "n_clicks"),
    State("task-ids-store", "data"),
    prevent_initial_call=True
)
def remove_task(_, stored_ids):
    btn = ctx.triggered_id
    if not btn or not isinstance(btn, dict):
        return stored_ids
    tid = btn.get("index")
    if not tid:
        return stored_ids
    if stored_ids and tid in stored_ids:
        task = tm.get_task(tid)
        if task and hasattr(task, '_chart_cache'):
            task._chart_cache.clear()  # Free RAM before deletion
        tm.remove_task(tid)
        return [x for x in stored_ids if x != tid]
    return stored_ids

# Note: The JavaScript event listener at line 2578 handles DIV button clicks globally
# No need for a separate clientside_callback for remove-task buttons

# 🔧 CRITICAL: Clientside callback to handle DIV button clicks and trigger server-side callbacks
# This converts DIV clicks into store updates that server callbacks can listen to
clientside_callback(
    """
function(clickData) {
    // This is a dummy callback to enable DIV click handling via the existing JS event listener
    // The actual work is done by the JavaScript event listener at line 2578
    return window.dash_clientside.no_update;
}
""",
    Output("div-click-dummy-store", "data"),
    Input("div-click-trigger-store", "data"),
    prevent_initial_call=False
)



@app.callback(
    Output({"type": "log", "index": ALL}, "value"),
    Output({"type": "progress", "index": ALL}, "value"),
    Output({"type": "progress-text", "index": ALL}, "children"),
    Input("progress-interval", "n_intervals"),
    State({"type": "task-store", "index": ALL}, "data")
)
def update_progress(_, stores):
    if not stores:
        return [], [], []
    logs, progs, texts = [], [], []
    for s in stores:
        tid = s.get("props", {}).get("data-task_id") if isinstance(s, dict) else None
        if not tid:
            tid = s.get("data", {}).get("task_id") if isinstance(s, dict) else None
        if not tid:
            logs.append("")
            progs.append("0")
            texts.append("0.0% 0/0/0")
            continue
        task = tm.get_task(tid)
        if task:
            logs.append("\n".join(task.log) if task.log else "No logs yet...")
            progs.append(str(task.progress))
            rem = max(0, task.total_candles - task.downloaded_candles) if task.total_candles else 0
            texts.append(f"{task.progress:.1f}%  {task.downloaded_candles}/{task.total_candles}/{rem}")
        else:
            logs.append("")
            progs.append("0")
            texts.append("0.0% 0/0/0")
    return logs, progs, texts

# ============================================================================
# 🔧 SPLIT CALLBACK #1: Summary Statistics Only (HEAVY - runs ONCE per data load)
# ============================================================================


@app.callback(
    Output("summary-stats-container", "children"),
    Input("golden-store-version", "data"),  # ✅ FIXED: Only trigger when data version changes (not on page clicks)
    Input("recalc-lock-store", "data")
)
def update_summary_stats_only(version, lock_state):
    """Calculate summary statistics ONLY when golden_store_version changes.
    Does NOT run on page navigation - this is the key fix for 10-minute freeze."""
    global golden_task_store_data, golden_store_version, recalculation_complete_timestamp
    
    # Validate global state
    if not hasattr(app, 'layout') or app.layout is None:
        return html.Div("", style={"display": "none"})
    
    # Get tasks from dcc.Store via callback context or fallback to global
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update
        
    # Check if version changed (to avoid recalc on lock state changes alone)
    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]
    if triggered_id == "recalc-lock-store":
        return dash.no_update  # Don't recalc stats just because lock changed
        
    # Try to get data from store first, fallback to global
    try:
        # In a real dcc.Store setup, we'd get this from Input, but for now use global
        tasks = golden_task_store_data if golden_task_store_data else (list(tm.tasks.values()) if hasattr(tm, 'tasks') else [])
    except:
        tasks = []
    
    if not tasks:
        return html.Div("⏳ Initializing...", style={"textAlign": "center", "padding": "20px", "color": "#666"})
    
    # Lock check
    if lock_state and lock_state.get("locked", False):
        return html.Div([
            html.Div("⏳ Recalculating... Please wait", style={"textAlign": "center", "padding": "20px", "fontSize": "16px", "color": "#666"}),
            html.Div(lock_state.get("message", ""), style={"textAlign": "center", "fontSize": "12px", "color": "#999"})
        ])
    
    # Get tasks from Golden Store
    if golden_task_store_data is not None and len(golden_task_store_data) > 0:
        tasks = golden_task_store_data
    else:
        with tm.lock:
            tasks = list(tm.tasks.values())
        
    if not tasks:
        return "No tasks."
    
    # ✅ BASIC STATS: Clear separation of Completed vs Total Tasks
    total_tasks = len(tasks)
    completed_count = sum(1 for t in tasks if t.status == "completed")
    
    # ✅ FIXED: Removed page-specific averages from stats (they were causing confusion)
    # Stats now show GLOBAL averages across ALL tasks, not just visible page
    avg_adv = np.mean([t.max_adverse_move_pct for t in tasks if t.max_adverse_move_pct is not None and not pd.isna(t.max_adverse_move_pct)] or [0])
    avg_dd = np.mean([t.drawdown_before_level for t in tasks if t.drawdown_before_level is not None and not pd.isna(t.drawdown_before_level)] or [0])
    
    stats_rows = [
        html.Tr([html.Td("✅ Task Completed 100%"), html.Td(str(completed_count))]),
        html.Tr([html.Td("📦 Total Task"), html.Td(str(total_tasks))]),
        html.Tr([html.Td("📉 Avg Max Adverse (Global)"), html.Td(f"{avg_adv:.2f}%")]),
        html.Tr([html.Td("📉 Avg Drawdown Lvl (Global)"), html.Td(f"{avg_dd:.2f}%")])
    ]
    stats_table = html.Table([html.Tbody(stats_rows)], style={"border": "1px solid #ccc", "padding": "5px", "fontSize": "13px", "backgroundColor": "#f9f9f9"})
    
    # ✅ SIGNAL STATS: Calculated on ALL in-memory tasks (consistent denominator)
    reached_level_cnt = sum(1 for t in tasks if getattr(t, 'reached_level', False))
    reversed_dir_cnt = sum(1 for t in tasks if getattr(t, 'reversed_direction', False))
    hit_1_cnt = sum(1 for t in tasks if getattr(t, 'reached_level', False) and getattr(t, 'hit_1', False))
    hit_1_5_cnt = sum(1 for t in tasks if getattr(t, 'reached_level', False) and getattr(t, 'hit_1_5', False))
    hit_2_cnt = sum(1 for t in tasks if getattr(t, 'reached_level', False) and getattr(t, 'hit_2', False))
    
    def fmt_stat(stat_count, total):
        if total == 0: return "0 / 0 (0.0%)"
        return f"{stat_count} / {total} ({(stat_count/total)*100:.1f}%)"

    # ----- Max Adverse Distribution Stats -----
    def get_adverse_range(pct):
        if pct is None or (isinstance(pct, float) and pd.isna(pct)):
            return None
        if 0 <= pct < 0.5: return "0-0.5%"
        elif 0.5 <= pct < 1: return "0.5-1%"
        elif 1 <= pct < 2: return "1-2%"
        elif 2 <= pct < 3: return "2-3%"
        elif 3 <= pct < 4: return "3-4%"
        elif 4 <= pct < 5: return "4-5%"
        elif 5 <= pct < 10: return "5-10%"
        elif 10 <= pct < 20: return "10-20%"
        elif 20 <= pct < 30: return "20-30%"
        elif pct >= 30: return ">30%"
        return None

    adverse_counts = {}
    for t in tasks:
        adv = getattr(t, 'max_adverse_move_pct', None)
        if t.reached_level and adv is not None and not (isinstance(adv, float) and pd.isna(adv)):
            range_key = get_adverse_range(adv)
            if range_key:
                adverse_counts[range_key] = adverse_counts.get(range_key, 0) + 1

    ranges = ["0-0.5%", "0.5-1%", "1-2%", "2-3%", "3-4%", "4-5%", "5-10%", "10-20%", "20-30%", ">30%"]
    row1_adv = " | ".join([f"{r}:{adverse_counts.get(r,0)}" for r in ranges[:5]])
    row2_adv = " | ".join([f"{r}:{adverse_counts.get(r,0)}" for r in ranges[5:]])

    adv_05_plus_total = 0
    adv_4_plus_total = 0
    for t in tasks:
        adv = getattr(t, 'max_adverse_move_pct', None)
        if t.reached_level and adv is not None and not (isinstance(adv, float) and pd.isna(adv)):
            if adv >= 0.5:
                adv_05_plus_total += 1
            if adv >= 4.0:
                adv_4_plus_total += 1

    exp_counts = {}
    exp_05_plus_total = 0
    exp_4_plus_total = 0
    for t in tasks:
        exp = getattr(t, 'max_expected_move_pct', None)
        if t.reached_level and exp is not None and not (isinstance(exp, float) and pd.isna(exp)):
            range_key = get_adverse_range(exp)
            if range_key:
                exp_counts[range_key] = exp_counts.get(range_key, 0) + 1
            if exp >= 0.5:
                exp_05_plus_total += 1
            if exp >= 4.0:
                exp_4_plus_total += 1
                
    row1_exp = " | ".join([f"{r}:{exp_counts.get(r,0)}" for r in ranges[:5]])
    row2_exp = " | ".join([f"{r}:{exp_counts.get(r,0)}" for r in ranges[5:]])

    td_style = {"fontSize": "13px", "fontWeight": "normal", "padding": "2px 5px"}
    
    adv_sgnl_counts = {}; exp_sgnl_counts = {}
    adv_sgnl_05 = 0; adv_sgnl_4 = 0; exp_sgnl_05 = 0; exp_sgnl_4 = 0
    for t in tasks:
        adv_s = getattr(t, 'max_adverse_sgnl_pct', None)
        if adv_s is not None and not (isinstance(adv_s, float) and pd.isna(adv_s)):
            r = get_adverse_range(adv_s)
            if r: adv_sgnl_counts[r] = adv_sgnl_counts.get(r, 0) + 1
            if adv_s >= 0.5: adv_sgnl_05 += 1
            if adv_s >= 4.0: adv_sgnl_4 += 1
        exp_s = getattr(t, 'max_expected_sgnl_pct', None)
        if exp_s is not None and not (isinstance(exp_s, float) and pd.isna(exp_s)):
            r = get_adverse_range(exp_s)
            if r: exp_sgnl_counts[r] = exp_sgnl_counts.get(r, 0) + 1
            if exp_s >= 0.5: exp_sgnl_05 += 1
            if exp_s >= 4.0: exp_sgnl_4 += 1
            
    row1_adv_s = " | ".join([f"{r}:{adv_sgnl_counts.get(r,0)}" for r in ranges[:5]])
    row2_adv_s = " | ".join([f"{r}:{adv_sgnl_counts.get(r,0)}" for r in ranges[5:]])
    row1_exp_s = " | ".join([f"{r}:{exp_sgnl_counts.get(r,0)}" for r in ranges[:5]])
    row2_exp_s = " | ".join([f"{r}:{exp_sgnl_counts.get(r,0)}" for r in ranges[5:]])
    
    delta_counts = {k: 0 for k in ranges}
    delta_05_plus_total = 0
    delta_4_plus_total = 0
    for t in tasks:
        dp = getattr(t, 'price_change_pct', None)
        if dp is not None and not (isinstance(dp, float) and pd.isna(dp)):
            val = abs(dp)
            r = get_adverse_range(val)
            if r:
                delta_counts[r] += 1
            if val >= 0.5: delta_05_plus_total += 1
            if val >= 4.0: delta_4_plus_total += 1

    row1_delta = " | ".join([f"{r}:{delta_counts[r]}" for r in ranges[:5]])
    row2_delta = " | ".join([f"{r}:{delta_counts[r]}" for r in ranges[5:]])

    signal_stats_rows = [
        html.Tr([html.Td("Reached Level", style=td_style), html.Td(fmt_stat(reached_level_cnt, total_tasks), style=td_style)]),
        html.Tr([html.Td("Reversed Direction", style=td_style), html.Td(fmt_stat(reversed_dir_cnt, total_tasks), style=td_style)]),
        html.Tr([html.Td("Hit 1% (from level)", style=td_style), html.Td(fmt_stat(hit_1_cnt, total_tasks), style=td_style)]),
        html.Tr([html.Td("Hit 1.5% (from level)", style=td_style), html.Td(fmt_stat(hit_1_5_cnt, total_tasks), style=td_style)]),
        html.Tr([html.Td("Hit 2% (from level)", style=td_style), html.Td(fmt_stat(hit_2_cnt, total_tasks), style=td_style)]),
        html.Tr([html.Td("Max Adv 0-4% (lvl)", style=td_style), html.Td(row1_adv, style=td_style)]),
        html.Tr([html.Td("Max Adv 4%+ (lvl)", style=td_style), html.Td(row2_adv, style=td_style)]),
        html.Tr([html.Td("Max Adv 0.5%+ Total (lvl)", style=td_style), html.Td(str(adv_05_plus_total), style=td_style)]),
        html.Tr([html.Td("Max Adv 4%+ Total (lvl)", style=td_style), html.Td(str(adv_4_plus_total), style=td_style)]),
        html.Tr([html.Td("Max Exp 0-4% (lvl)", style=td_style), html.Td(row1_exp, style=td_style)]),
        html.Tr([html.Td("Max Exp 4%+ (lvl)", style=td_style), html.Td(row2_exp, style=td_style)]),
        html.Tr([html.Td("Max Exp 0.5%+ Total (lvl)", style=td_style), html.Td(str(exp_05_plus_total), style=td_style)]),
        html.Tr([html.Td("Max Exp 4%+ Total (lvl)", style=td_style), html.Td(str(exp_4_plus_total), style=td_style)]),
        html.Tr([html.Td("Max Adv 0-4% (sgnl)", style=td_style), html.Td(row1_adv_s, style=td_style)]),
        html.Tr([html.Td("Max Adv 4%+ (sgnl)", style=td_style), html.Td(row2_adv_s, style=td_style)]),
        html.Tr([html.Td("Max Adv 0.5%+ Total (sgnl)", style=td_style), html.Td(str(adv_sgnl_05), style=td_style)]),
        html.Tr([html.Td("Max Adv 4%+ Total (sgnl)", style=td_style), html.Td(str(adv_sgnl_4), style=td_style)]),
        html.Tr([html.Td("Max Exp 0-4% (sgnl)", style=td_style), html.Td(row1_exp_s, style=td_style)]),
        html.Tr([html.Td("Max Exp 4%+ (sgnl)", style=td_style), html.Td(row2_exp_s, style=td_style)]),
        html.Tr([html.Td("Max Exp 0.5%+ Total (sgnl)", style=td_style), html.Td(str(exp_sgnl_05), style=td_style)]),
        html.Tr([html.Td("Max Exp 4%+ Total (sgnl)", style=td_style), html.Td(str(exp_sgnl_4), style=td_style)]),
        html.Tr([html.Td("Delta Price 0-4%", style=td_style), html.Td(row1_delta, style=td_style)]),
        html.Tr([html.Td("Delta Price 4%+", style=td_style), html.Td(row2_delta, style=td_style)]),
        html.Tr([html.Td("Delta Price 0.5%+ Total", style=td_style), html.Td(str(delta_05_plus_total), style=td_style)]),
        html.Tr([html.Td("Delta Price 4%+ Total", style=td_style), html.Td(str(delta_4_plus_total), style=td_style)]),
    ]
    signal_stats_table = html.Table([html.Tbody(signal_stats_rows)], style={"border": "1px solid #4a90e2", "padding": "5px", "marginTop": "10px", "backgroundColor": "#f0f7ff"})
    
    return html.Div([
        stats_table,
        html.H5("Signal Performance Summary", style={"marginTop": "15px", "marginBottom": "5px"}),
        signal_stats_table,
        html.P(
            "ℹ️ Hit % metrics measure price movement ≥1%/1.5%/2% **in the EXPECTED direction** from the signal level base. "
            "Resistance: Price moves UP ≥X% from level. Support: Price moves DOWN ≥X% from level. "
            "Hits are only counted if the price actually touched the level first.",
            style={"fontSize": "11px", "color": "#777", "marginTop": "6px", "marginBottom": "0", "fontStyle": "italic"}
        )
    ])


# ============================================================================
# 🔧 SPLIT CALLBACK #2: Task Table Only (LIGHT - runs on every page click)
# ============================================================================

# ⚡ CRITICAL OPTIMIZATION: Page-level HTML cache
# Stores pre-rendered HTML rows for each page to avoid re-rendering on navigation
_page_html_cache = {}
_cached_golden_version = None



@app.callback(
    Output("task-page-store", "data"),
    Input({"type": "page-nav", "index": ALL}, "n_clicks"),
    State("task-count-store", "data"),
    State("task-page-store", "data"),
    prevent_initial_call=True
)
def handle_page_nav(n_clicks_list, count, current_page):
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return current_page
    action = triggered.get("index")
    total_tasks = int(count) if count else len(tm.get_all_tasks())
    total_pages = max(1, (total_tasks + PAGE_SIZE - 1) // PAGE_SIZE)
    
    if action == "prev":
        return max(0, current_page - 1)
    elif action == "next":
        return min(total_pages - 1, current_page + 1)
    elif isinstance(action, int):
        return action
    return current_page



@app.callback(
    Output("progress-interval", "disabled"),
    Output("analysis-interval", "disabled"),  # 🔧 Enable analysis-interval during recalc
    Input("progress-interval", "n_intervals"),
    prevent_initial_call=True
)
def auto_throttle_updates(_):
    """Keep interval always enabled. 
    The 'update_summary' callback handles performance by returning 'no_update' 
    when the table hasn't actually changed."""
    return False, False  # 🔧 Keep both intervals enabled

# ----- NEW: Callback for chart button using data-action pattern -----
# This callback listens to the hidden trigger that JS sets when chart button is clicked


@app.callback(
    Output("chart-task-id", "data"),
    Output("chart-click-store", "data"),
    Input("chart-button-trigger", "data"),  # Hidden trigger set by JS
    State("chart-click-store", "data"),
    prevent_initial_call=True
)
def set_chart_task_id(trigger_data, click_store):
    if not trigger_data:
        return no_update, no_update
    
    task_id = trigger_data.get("task_id")
    action = trigger_data.get("action")
    
    if not task_id or action != "chart":
        return no_update, no_update
    
    # Deduplication logic
    key = f"{task_id}_chart"
    current_time = time.time()
    old_time = click_store.get(key, 0)
    
    # Only process if this is a new click (within 0.5 seconds)
    if current_time - old_time < 0.5:
        return no_update, no_update
    
    click_store[key] = current_time
    return task_id, click_store

# ----- Modal display callback -----


@app.callback(
    Output("chart-modal", "style"),
    Input("chart-task-id", "data"),
    Input("close-chart-modal", "n_clicks"),
    prevent_initial_call=True
)
def toggle_chart_modal(task_id, close_clicks):
    triggered = ctx.triggered_id
    if triggered == "close-chart-modal":
        return {"display": "none"}
    if task_id:
        return {"display": "flex"}
    return no_update



@app.callback(
    Output("rsi-visible-store", "data"),
    Input("toggle-rsi-btn", "n_clicks"),
    State("rsi-visible-store", "data"),
    prevent_initial_call=True
)
def toggle_rsi(n_clicks, current):
    return not current



@app.callback(
    Output("strategy-visible-store", "data"),
    Input("toggle-strategy-btn", "n_clicks"),
    State("strategy-visible-store", "data"),
    prevent_initial_call=True
)
def toggle_strategy(n_clicks, current):
    return not current

# ----- Measurement tool callbacks -----


@app.callback(
    Output("measure-mode-store", "data"),
    Input("toggle-measure-btn", "n_clicks"),
    State("measure-mode-store", "data"),
    prevent_initial_call=True
)
def toggle_measure(n_clicks, current):
    return not current



@app.callback(
    Output("measure-points-store", "data"),
    Output("measure-result-store", "data"),
    Input("task-chart", "clickData"),
    State("measure-mode-store", "data"),
    State("measure-points-store", "data"),
    prevent_initial_call=True
)
def capture_click(clickData, measure_mode, points):
    if not measure_mode or not clickData:
        return dash.no_update, dash.no_update
    try:
        x_val = clickData['points'][0]['x']
        y_val = clickData['points'][0]['y']
        if points['first'] is None:
            # First click
            return {"first": {"x": x_val, "y": y_val}, "second": None}, None
        else:
            # Second click
            first = points['first']
            second = {"x": x_val, "y": y_val}
            price_diff = second['y'] - first['y']
            pct_change = (price_diff / first['y']) * 100
            result = f"📏 Δ Price: {price_diff:+.4f} ({pct_change:+.2f}%)"
            return {"first": None, "second": None}, result
    except Exception:
        return dash.no_update, dash.no_update



@app.callback(
    Output("measure-points-store", "data", allow_duplicate=True),
    Output("measure-result-store", "data", allow_duplicate=True),
    Input("measure-mode-store", "data"),
    prevent_initial_call=True
)
def reset_measure_on_mode_exit(mode):
    if not mode:
        return {"first": None, "second": None}, None
    return dash.no_update, dash.no_update



@app.callback(
    Output("measure-result", "children"),
    Input("measure-result-store", "data"),
    prevent_initial_call=True
)
def show_measure_result(result):
    if result:
        return result
    return ""



@app.callback(
    Output("measure-hint", "children"),
    Input("measure-mode-store", "data"),
    prevent_initial_call=True
)
def measure_hint(active):
    if active:
        return "📏 Measure mode active: click two points on the chart to measure price difference."
    return ""

# ----- Strategy details modal callbacks (using data-action pattern) -----


@app.callback(
    Output("strategy-details-task-id", "data"),
    Output("details-click-store", "data"),
    Input("strategy-details-trigger", "data"),  # Hidden trigger set by JS
    State("details-click-store", "data"),
    prevent_initial_call=True
)
def set_strategy_details_task_id(trigger_data, click_store):
    if not trigger_data:
        return no_update, no_update
    
    task_id = trigger_data.get("task_id")
    if not task_id:
        return no_update, no_update
    
    # Deduplication logic
    key = f"{task_id}_details"
    current_time = time.time()
    old_time = click_store.get(key, 0)
    
    if current_time - old_time < 0.5:
        return no_update, no_update
    
    click_store[key] = current_time
    return task_id, click_store



@app.callback(
    Output("strategy-details-modal", "style"),
    Output("strategy-details-title", "children"),
    Output("strategy-details-content", "children"),
    Input("strategy-details-task-id", "data"),
    Input("close-strategy-details-modal", "n_clicks"),
    prevent_initial_call=True
)
def toggle_strategy_details_modal(task_id, close_clicks):
    triggered = ctx.triggered_id
    if triggered == "close-strategy-details-modal":
        return {"display": "none"}, "", ""
    if task_id is None:
        return no_update, no_update, no_update
    task = tm.get_task(task_id)
    if not task or not task.strategy_signals:
        return {"display": "flex"}, f"Task {task_id[:8]} – No strategy signals", html.P("No strategy signals for this task.")
    # Build table of signals with entry/exit prices and times
    rows = []
    for sig in task.strategy_signals:
        entry_time = pd.to_datetime(sig['entry_time_ms'], unit='ms', utc=True).strftime("%Y-%m-%d %H:%M")
        exit_time = pd.to_datetime(sig['exit_time_ms'], unit='ms', utc=True).strftime("%Y-%m-%d %H:%M") if sig.get('exit_time_ms') else "-"
        pnl = sig.get('delta_pct')
        if pnl is None:
            pnl = 0.0
        pnl_color = "green" if pnl > 0 else "red" if pnl < 0 else "white"
        rows.append(html.Tr([
            html.Td(entry_time),
            html.Td(sig['type'].capitalize()),
            html.Td(sig['direction'].upper()),
            html.Td(f"{sig['entry_price']:.4f}"),
            html.Td(f"{sig['exit_price']:.4f}") if sig.get('exit_price') is not None else html.Td("-"),
            html.Td(exit_time),
            html.Td(f"{sig['confidence']:.0f}%"),
            html.Td(f"{pnl:+.2f}%", style={"color": pnl_color}),
            html.Td(sig.get('extra_info', '-'), style={"maxWidth": "200px", "fontSize": "12px"})  # new column
        ]))
    table = html.Table([
        html.Thead(html.Tr([
            html.Th("Entry Time (UTC)"), html.Th("Type"), html.Th("Dir"),
            html.Th("Entry Price"), html.Th("Exit Price"), html.Th("Exit Time (UTC)"),
            html.Th("Confidence"), html.Th("P&L %"), html.Th("Reason / Parameters")
        ])),
        html.Tbody(rows)
    ], style={"width": "100%", "border": "1px solid gray", "borderCollapse": "collapse"})
    # Win rates per strategy type
    from collections import defaultdict
    stats = defaultdict(lambda: {"total": 0, "win": 0})
    for sig in task.strategy_signals:
        t = sig['type']
        stats[t]["total"] += 1
        delta = sig.get('delta_pct')
        if delta is not None and delta > 0:
            stats[t]["win"] += 1
    stats_rows = []
    for t, data in stats.items():
        win_rate = (data["win"] / data["total"] * 100) if data["total"] > 0 else 0
        stats_rows.append(html.Tr([
            html.Td(t.capitalize()),
            html.Td(data["total"]),
            html.Td(data["win"]),
            html.Td(f"{win_rate:.1f}%")
        ]))
    stats_table = html.Table([
        html.Thead(html.Tr([html.Th("Strategy"), html.Th("Total"), html.Th("Wins"), html.Th("Win Rate")])),
        html.Tbody(stats_rows)
    ], style={"width": "50%", "border": "1px solid gray", "borderCollapse": "collapse", "marginTop": "10px"})
    content = html.Div([html.Div(table, style={"overflow-x": "auto"}), stats_table])
    title = f"Strategy Signals – {task.symbols[0]} ({task.timeframe})"
    return {"display": "flex"}, title, content

# ----- Chart figure callback (light theme) -----


@app.callback(
    Output("task-chart", "figure"),
    Input("chart-task-id", "data"),
    Input("rsi-visible-store", "data"),
    Input("strategy-visible-store", "data"),
    Input("impulse-visible-store", "data"),
    Input("events-visible-store", "data"),
    prevent_initial_call=True
)
def update_task_chart(task_id, rsi_visible, strategy_visible, impulse_visible, events_visible):
    if not task_id:
        return go.Figure()
    task = tm.get_task(task_id)
    if not task or not task.signal_time:
        return go.Figure()
    # Load data
    sym = task.symbols[0]
    path = symbol_timeframe_path(sym, task.timeframe)
    fp = os.path.join(path, "data.parquet")
    if not os.path.exists(fp):
        return go.Figure()
    df = pd.read_parquet(fp)
    if df.empty:
        return go.Figure()
    # Filter period
    start_ms = int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000) if task.start_date else 0
    end_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) if task.end_date else df['timestamp'].max()
    df = df[(df['timestamp'] >= start_ms) & (df['timestamp'] <= end_ms)].copy()
    if df.empty:
        return go.Figure()
    # UTC datetime conversion
    def ms_to_utc_datetime(ms):
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    df['x'] = df['timestamp'].apply(ms_to_utc_datetime)
    signal_dt = ms_to_utc_datetime(task.signal_time)
    # RSI calculation
    def compute_rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    # Low-spec chart cache: compute RSI once per period view
    cache_key = (start_ms, end_ms)
    if cache_key not in task._chart_cache:
        df['rsi'] = compute_rsi(df['close'])
        task._chart_cache.clear()  # Keep only 1 view in RAM
        task._chart_cache[cache_key] = df.copy()
    else:
        df = task._chart_cache[cache_key]
    # Create figure
    if rsi_visible:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.05, row_heights=[0.7, 0.3])
        # Candlestick
        fig.add_trace(go.Candlestick(
            x=df['x'], open=df['open'], high=df['high'],
            low=df['low'], close=df['close'], name="OHLC",
            increasing_line_color='#26a69a', decreasing_line_color='#ef5350'
        ), row=1, col=1)
        # RSI line
        fig.add_trace(go.Scatter(
            x=df['x'], y=df['rsi'], mode='lines', name='RSI (14)',
            line=dict(color='purple', width=1.5), connectgaps=True
        ), row=2, col=1)
        # Helper trace on RSI (ensures hover line works – kept for consistency)
        fig.add_trace(go.Scatter(
            x=df['x'], y=[50]*len(df), mode='lines',
            name='_spike_helper_rsi', showlegend=False, hoverinfo='skip',
            line=dict(width=1, color='rgba(0,0,0,0.01)')
        ), row=2, col=1)
        # RSI levels
        fig.add_hline(y=70, line_dash="dash", line_color="red", row=2, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="green", row=2, col=1)
        fig.update_yaxes(title_text="RSI", row=2, col=1, range=[0, 100])
    else:
        fig = make_subplots(rows=1, cols=1, shared_xaxes=True)
        fig.add_trace(go.Candlestick(
            x=df['x'], open=df['open'], high=df['high'],
            low=df['low'], close=df['close'], name="OHLC",
            increasing_line_color='#26a69a', decreasing_line_color='#ef5350'
        ))
        # Helper trace on main chart (ensures hover line works – kept)
        y_mid = (df['high'].max() + df['low'].min()) / 2
        fig.add_trace(go.Scatter(
            x=df['x'], y=[y_mid]*len(df), mode='lines',
            name='_spike_helper_main', showlegend=False, hoverinfo='skip',
            line=dict(width=1, color='rgba(0,0,0,0.01)')
        ), row=1, col=1)
    # Signal level
    signal_price = task.signal_price
    fig.add_hline(y=signal_price, line_dash="dash", line_color="yellow",
                  annotation_text="Signal Level", annotation_position="top right",
                  row=1, col=1)
    # Event markers (only if toggled on)
    if events_visible and hasattr(task, 'events') and task.events:
        for ev in task.events:
            ts = ev['timestamp']
            event_dt = ms_to_utc_datetime(ts)
            event_type = ev['type']
            color = 'magenta' if 'pin' in event_type else \
                'cyan' if 'touch' in event_type else \
                'orange' if 'bounce' in event_type else \
                'red' if 'breakthrough' in event_type else 'white'
            fig.add_trace(go.Scatter(
                x=[event_dt], y=[ev.get('close', signal_price)],
                mode='markers', marker=dict(size=10, color=color),
                name=event_type, showlegend=False
            ), row=1, col=1)
    # Y-range for main chart (with padding)
    y_min = df['low'].min()
    y_max = df['high'].max()
    y_padding = (y_max - y_min) * 0.05
    y_min -= y_padding
    y_max += y_padding
    # Signal vertical line (fixed white dashed line at signal time)
    fig.add_trace(go.Scatter(
        x=[signal_dt, signal_dt], y=[y_min, y_max],
        mode='lines', line=dict(dash='dash', color='white', width=1),
        name='Signal Time', showlegend=False
    ), row=1, col=1)
    # Signal diamond marker
    fig.add_trace(go.Scatter(
        x=[signal_dt], y=[task.signal_price],
        mode='markers',
        marker=dict(size=10, color='white', symbol='diamond', line=dict(width=1, color='yellow')),
        name='Signal Time Marker', showlegend=False
    ), row=1, col=1)
    # ----- Strategy markers (separate: impulse vs other) -----
    if hasattr(task, 'strategy_signals') and task.strategy_signals:
        # Non‑impulse signals (bounce, retest, momentum) – only if strategy_visible is True
        if strategy_visible:
            for sig in task.strategy_signals:
                if sig['type'] == 'impulse':
                    continue
                sig_time = ms_to_utc_datetime(sig['entry_time_ms'])
                if sig['direction'] == 'buy':
                    marker = dict(symbol='triangle-up', size=12, color='lime')
                else:
                    marker = dict(symbol='triangle-down', size=12, color='red')
                fig.add_trace(go.Scatter(
                    x=[sig_time], y=[sig['entry_price']],
                    mode='markers', marker=marker,
                    name=f"{sig['type']} {sig['direction']}",
                    showlegend=False
                ), row=1, col=1)
        # Impulse signals – only if impulse_visible is True
        if impulse_visible:
            for sig in task.strategy_signals:
                if sig['type'] != 'impulse':
                    continue
                sig_time = ms_to_utc_datetime(sig['entry_time_ms'])
                marker = dict(symbol='diamond', size=14, color='purple')
                fig.add_trace(go.Scatter(
                    x=[sig_time], y=[sig['entry_price']],
                    mode='markers', marker=marker,
                    name=f"Impulse {sig['direction']}",
                    showlegend=False,
                    text=sig.get('extra_info', ''),
                    hoverinfo='text+y'
                ), row=1, col=1)
    # Layout (light theme)
    fig.update_layout(
        title=f"{sym} – {task.timeframe}  (Signal at {pd.to_datetime(task.signal_time, unit='ms')})",
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        hovermode="x unified",
        height=700 if rsi_visible else 500,
        margin=dict(l=50, r=50, t=50, b=50)
    )
    # X-axis tick format
    fig.update_xaxes(tickformat="%H:%M", ticklabelmode="period", ticks="outside")
    return fig

# ----- Verification callbacks (unchanged) -----


@app.callback(
    Output("start-verify-btn", "disabled"),
    Output("start-deep-verify-btn", "disabled"),
    Output("stop-verify-btn", "disabled"),
    Input("start-verify-btn", "n_clicks"),
    Input("start-deep-verify-btn", "n_clicks"),
    Input("stop-verify-btn", "n_clicks"),
    prevent_initial_call=True
)
def control_verification(start_clicks, deep_clicks, stop_clicks):
    triggered = ctx.triggered_id
    if triggered == "start-verify-btn" and not vm.running:
        vm.start_verification(deep=False)
        return True, True, False
    elif triggered == "start-deep-verify-btn" and not vm.running:
        vm.start_verification(deep=True)
        return True, True, False
    elif triggered == "stop-verify-btn" and vm.running:
        vm.stop_verification()
        return no_update, no_update, no_update
    return no_update, no_update, no_update



@app.callback(
    Output("start-verify-btn", "disabled", allow_duplicate=True),
    Output("start-deep-verify-btn", "disabled", allow_duplicate=True),
    Output("stop-verify-btn", "disabled", allow_duplicate=True),
    Input("verify-interval", "n_intervals"),
    prevent_initial_call=True
)
def update_button_states(_):
    if not vm.running:
        return False, False, True
    return True, True, False



@app.callback(
    Output("verify-log", "children"),
    Input("verify-interval", "n_intervals")
)
def update_verify_log(_):
    return vm.get_logs()



@app.callback(
    Output("download-report", "data"),
    Input("generate-report-btn", "n_clicks"),
    prevent_initial_call=True
)
def generate_report(_):
    report = vm.generate_integrity_report()
    report_str = json.dumps(report, indent=2)
    return dcc.send_string(report_str, f"integrity_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")



@app.callback(
    Output("duckdb-result", "children"),
    Input("run-duckdb-btn", "n_clicks"),
    prevent_initial_call=True
)
def run_duckdb_query(_):
    if not DUCKDB_AVAILABLE:
        return "DuckDB not installed. Please run: pip install duckdb"
    try:
        conn = duckdb.connect()
        query = """
        SELECT
        regexp_extract(filename, 'market_data/([^/]+)/', 1) as symbol,
        COUNT(*) as candle_count,
        MIN(timestamp) as earliest,
        MAX(timestamp) as latest,
        AVG(close) as avg_close,
        STDDEV(close) as volatility,
        SUM(volume) as total_volume
        FROM read_parquet('market_data/*/60/data.parquet', filename=true)
        GROUP BY symbol
        ORDER BY symbol
        """
        df = conn.execute(query).df()
        return df.to_string()
    except Exception as e:
        return f"Error: {e}"



@app.callback(
    Output("chart-timeframe-dropdown", "options"),
    Input("chart-symbol-dropdown", "value")
)
def update_timeframe_options(selected_symbol):
    if not selected_symbol:
        return []
    info = get_database_info()
    timeframes = sorted(set(
        d["timeframe"] for d in info["details"] if d["symbol"] == selected_symbol
    ))
    return [{"label": tf, "value": tf} for tf in timeframes]



@app.callback(
    Output("candlestick-chart", "figure"),
    Input("chart-symbol-dropdown", "value"),
    Input("chart-timeframe-dropdown", "value")
)
def update_chart(symbol, timeframe):
    if not symbol or not timeframe:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.05, row_heights=[0.7, 0.3])
        fig.update_layout(title="Select a symbol and timeframe to view chart")
        return fig
    path = symbol_timeframe_path(symbol, timeframe)
    file_path = os.path.join(path, "data.parquet")
    if not os.path.exists(file_path):
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.05, row_heights=[0.7, 0.3])
        fig.update_layout(title=f"No data for {symbol} {timeframe}")
        return fig
    df = pd.read_parquet(file_path)
    if df.empty:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.05, row_heights=[0.7, 0.3])
        fig.update_layout(title=f"Empty data for {symbol} {timeframe}")
        return fig
    df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.05, row_heights=[0.7, 0.3])
    fig.add_trace(go.Candlestick(
        x=df['date'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name="OHLC",
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350'
    ), row=1, col=1)
    colors = ['#26a69a' if row['close'] >= row['open'] else '#ef5350' for _, row in df.iterrows()]
    fig.add_trace(go.Bar(
        x=df['date'],
        y=df['volume'],
        name="Volume",
        marker_color=colors,
        showlegend=False
    ), row=2, col=1)
    fig.update_layout(
        title=f"{symbol} – {timeframe}",
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        hovermode="x unified",
        height=600,
        margin=dict(l=50, r=50, t=50, b=50)
    )
    fig.update_xaxes(title_text="Date", row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return fig



@app.callback(Output("download-db", "data"),
              Input("download-db-btn", "n_clicks"),
              prevent_initial_call=True)
def backup(_):
    zip_name = f"market_data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    shutil.make_archive(zip_name.replace('.zip', ''), 'zip', MARKET_DATA_DIR)
    return dcc.send_file(zip_name)

# ----- Impulse callbacks -----


@app.callback(
    Output("impulse-task-selector", "options"),
    Input("progress-interval", "n_intervals")
)
def update_impulse_task_selector(_):
    tasks = tm.get_all_tasks()
    return [{"label": f"{t.task_id[:8]} - {t.symbols[0]} ({t.timeframe})", "value": t.task_id} for t in tasks if t.status == "completed"]



@app.callback(
    Output("impulse-apply-status", "children"),
    Input("apply-impulse-params", "n_clicks"),
    State("impulse-task-selector", "value"),
    State("impulse-range-mult", "value"),
    State("impulse-vol-mult", "value"),
    State("impulse-body-ratio", "value"),
    State("impulse-wick-ratio", "value"),
    State("impulse-next-confirm", "value"),
    State("impulse-rsi-divergence", "value"),
    State("impulse-rsi-extreme", "value"),
    State("impulse-base-candle", "value"),
    State("impulse-vol-accel", "value"),
    State("impulse-use-retracement", "value"),
    prevent_initial_call=True
)
def apply_impulse_params(n_clicks, task_id, range_mult, vol_mult, body_ratio, wick_ratio,
                         next_confirm, rsi_div, rsi_extreme, base_candle, vol_accel, use_retracement):
    if not task_id:
        return "No task selected."
    task = tm.get_task(task_id)
    if not task:
        return "Task not found."
    params = {
        'range_mult': range_mult,
        'vol_mult': vol_mult,
        'body_ratio': body_ratio,
        'wick_ratio': wick_ratio,
        'use_next_candle_confirmation': 'confirm' in next_confirm if next_confirm else False,
        'use_rsi_divergence': 'div' in rsi_div if rsi_div else False,
        'rsi_extreme': rsi_extreme,
        'use_base_candle': 'base' in base_candle if base_candle else False,
        'use_volume_acceleration': 'accel' in vol_accel if vol_accel else False,
    }
    try:
        from impulse import backtest_impulse, detect_impulse_retracement, set_impulse_params
        set_impulse_params(params)
        sym = task.symbols[0]
        path = symbol_timeframe_path(sym, task.timeframe)
        fp = os.path.join(path, "data.parquet")
        if not os.path.exists(fp):
            return "Data file not found."
        full_df = pd.read_parquet(fp)
        buffer_ms = task.pre_buffer_minutes * 60 * 1000
        start_ms = max(0, task.signal_time - buffer_ms)
        if task.start_date and task.end_date:
            window_len_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) - int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
            cutoff_time = task.signal_time + window_len_ms
            df_limited = full_df[(full_df['timestamp'] >= start_ms) & (full_df['timestamp'] <= cutoff_time)].copy()
        else:
            df_limited = full_df[full_df['timestamp'] >= start_ms].copy()
        if df_limited.empty:
            return "No data in selected period."
        use_retrace = "retrace" in (use_retracement or [])
        if use_retrace:
            # Use the pre-buffer value stored in the task (from task creation)
            buf = getattr(task, 'pre_buffer_minutes', 120)
            trades = detect_impulse_retracement(
                df_limited, task.signal_price, task.signal_direction, task.signal_time,
                pre_buffer_minutes=buf, verbose=False
            )
        else:
            res = backtest_impulse(
                df_limited, task.signal_price, task.signal_direction, task.signal_time,
                params=params, verbose=False
            )
            trades = res['trades']
        task.strategy_signals = [s for s in task.strategy_signals if s.get('type') != 'impulse']
        for trade in trades:
            task.add_strategy_signal(
                'impulse', trade['direction'], trade['entry_price'], trade['entry_time_ms'],
                exit_price=trade['exit_price'], exit_time_ms=trade['exit_time_ms'],
                confidence=trade.get('confidence', 60),
                extra_info=trade.get('parameters_log', trade.get('extra_info', ''))
            )
        task.add_log(f"Impulse detection completed: {len(trades)} signals (retracement={use_retrace})")
        return f"Applied. Impulse signals: {len(trades)} (retracement={use_retrace})"
    except Exception as e:
        return f"Error: {str(e)}"



@app.callback(
    Output("impulse-apply-all-status", "children"),
    Input("apply-impulse-all", "n_clicks"),
    State("impulse-range-mult", "value"),
    State("impulse-vol-mult", "value"),
    State("impulse-body-ratio", "value"),
    State("impulse-wick-ratio", "value"),
    State("impulse-next-confirm", "value"),
    State("impulse-rsi-divergence", "value"),
    State("impulse-rsi-extreme", "value"),
    State("impulse-base-candle", "value"),
    State("impulse-vol-accel", "value"),
    prevent_initial_call=True
)
def apply_impulse_to_all(n_clicks, range_mult, vol_mult, body_ratio, wick_ratio,
                         next_confirm, rsi_div, rsi_extreme, base_candle, vol_accel):
    if n_clicks == 0:
        return ""
    params = {
        'range_mult': range_mult,
        'vol_mult': vol_mult,
        'body_ratio': body_ratio,
        'wick_ratio': wick_ratio,
        'use_next_candle_confirmation': 'confirm' in next_confirm if next_confirm else False,
        'use_rsi_divergence': 'div' in rsi_div if rsi_div else False,
        'rsi_extreme': rsi_extreme,
        'use_base_candle': 'base' in base_candle if base_candle else False,
        'use_volume_acceleration': 'accel' in vol_accel if vol_accel else False,
    }
    tasks = tm.get_all_tasks()
    completed = [t for t in tasks if t.status == "completed"]
    if not completed:
        return "No completed tasks."
    success = 0
    total_impulse = 0
    for task in completed:
        try:
            cnt = task.run_impulse_detection(params=params, verbose=False)
            total_impulse += cnt
            success += 1
        except Exception as e:
            task.add_log(f"Impulse batch error: {e}")
    return f"Applied to {success} tasks. Total impulse signals: {total_impulse}"



@app.callback(
    Output("impulse-details-modal", "style"),
    Output("impulse-details-title", "children"),
    Output("impulse-details-content", "children"),
    Input({"type": "impulse-details-btn", "index": ALL}, "n_clicks"),
    State("details-click-store", "data"),
    prevent_initial_call=True
)
def show_impulse_details(n_clicks_list, click_store):
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update, no_update, no_update
    task_id = triggered.get("index")
    trig = ctx.triggered[0]
    new_clicks = trig.get('value', 0) or 0
    key = f"{task_id}_impulse"
    old_clicks = click_store.get(key, 0)
    if new_clicks <= old_clicks:
        return no_update, no_update, no_update
    click_store[key] = new_clicks
    task = tm.get_task(task_id)
    if not task or not task.strategy_signals:
        return {"display": "flex"}, f"Task {task_id[:8]} – No impulse signals", html.P("No impulse signals for this task.")
    impulse_signals = [s for s in task.strategy_signals if s['type'] == 'impulse']
    if not impulse_signals:
        return {"display": "flex"}, f"Task {task_id[:8]} – No impulse signals", html.P("No impulse signals.")
    rows = []
    for sig in impulse_signals:
        entry_time = pd.to_datetime(sig['entry_time_ms'], unit='ms').strftime("%Y-%m-%d %H:%M")
        exit_time = pd.to_datetime(sig['exit_time_ms'], unit='ms').strftime("%Y-%m-%d %H:%M") if sig.get('exit_time_ms') else "-"
        pnl = sig.get('delta_pct') if sig.get('delta_pct') is not None else 0.0
        pnl_color = "green" if pnl > 0 else "red" if pnl < 0 else "white"
        extra = sig.get('extra_info', '-')
        rows.append(html.Tr([
            html.Td(entry_time),
            html.Td(sig['direction'].upper()),
            html.Td(f"{sig['entry_price']:.5f}"),
            html.Td(f"{sig['exit_price']:.5f}") if sig.get('exit_price') is not None else html.Td("-"),
            html.Td(exit_time),
            html.Td(f"{sig['confidence']:.0f}%"),
            html.Td(f"{pnl:+.2f}%", style={"color": pnl_color}),
            html.Td(extra, style={"maxWidth": "250px", "fontSize": "12px"})
        ]))
    table = html.Table([
        html.Thead(html.Tr([
            html.Th("Entry Time"), html.Th("Dir"), html.Th("Entry Price"), html.Th("Exit Price"),
            html.Th("Exit Time"), html.Th("Confidence"), html.Th("P&L %"), html.Th("Parameters")
        ])),
        html.Tbody(rows)
    ], style={"width": "100%", "border": "1px solid gray", "borderCollapse": "collapse"})
    stats = {}
    for sig in impulse_signals:
        t = sig['type']
        stats.setdefault(t, {"total": 0, "win": 0})
        stats[t]["total"] += 1
        if sig.get('delta_pct', 0) > 0:
            stats[t]["win"] += 1
    stats_rows = []
    for t, data in stats.items():
        win_rate = (data["win"] / data["total"] * 100) if data["total"] > 0 else 0
        stats_rows.append(html.Tr([html.Td(t.capitalize()), html.Td(data["total"]), html.Td(data["win"]), html.Td(f"{win_rate:.1f}%")]))
    stats_table = html.Table([
        html.Thead(html.Tr([html.Th("Strategy"), html.Th("Total"), html.Th("Wins"), html.Th("Win Rate")])),
        html.Tbody(stats_rows)
    ], style={"width": "50%", "border": "1px solid gray", "borderCollapse": "collapse", "marginTop": "10px"})
    content = html.Div([table, stats_table])
    title = f"Impulse Signals – {task.symbols[0]} ({task.timeframe})"
    return {"display": "flex"}, title, content



@app.callback(
    Output("impulse-details-modal", "style", allow_duplicate=True),
    Input("close-impulse-details-modal", "n_clicks"),
    prevent_initial_call=True
)
def close_impulse_modal(n_clicks):
    return {"display": "none"}



@app.callback(
    Output("download-impulse-csv", "data"),
    Input("export-impulse-csv", "n_clicks"),
    State("impulse-details-title", "children"),
    prevent_initial_call=True
)
def export_impulse_csv(n_clicks, title):
    if not title:
        return None
    import re
    match = re.search(r"– (.+?) \(", title)
    if not match:
        return None
    sym = match.group(1).strip()
    tasks = tm.get_all_tasks()
    task = next((t for t in tasks if t.symbols[0] == sym), None)
    if not task:
        return None
    impulse_signals = [s for s in task.strategy_signals if s['type'] == 'impulse']
    if not impulse_signals:
        return None
    data = []
    for sig in impulse_signals:
        data.append({
            'Entry Time (UTC)': pd.to_datetime(sig['entry_time_ms'], unit='ms'),
            'Exit Time (UTC)': pd.to_datetime(sig['exit_time_ms'], unit='ms') if sig.get('exit_time_ms') else None,
            'Direction': sig['direction'],
            'Entry Price': sig['entry_price'],
            'Exit Price': sig.get('exit_price'),
            'Confidence': sig['confidence'],
            'P&L %': sig.get('delta_pct', 0),
            'Parameters': sig.get('extra_info', ''),
            'Exit Reason': sig.get('exit_reason', '')
        })
    df = pd.DataFrame(data)
    return dcc.send_data_frame(df.to_csv, f"impulse_signals_{sym}.csv", index=False)



@app.callback(
Output("impulse-results", "children"),
Output("processing-ops-store", "data", allow_duplicate=True),
Input("run-grid-search", "n_clicks"),
Input({"type": "grid-poll", "index": ALL}, "n_intervals"),
State("impulse-task-selector", "value"),
State("impulse-range-mult", "value"),
State("impulse-vol-mult", "value"),
State("impulse-body-ratio", "value"),
State("impulse-wick-ratio", "value"),
State("impulse-next-confirm", "value"),
State("impulse-rsi-divergence", "value"),
State("impulse-rsi-extreme", "value"),
State("impulse-base-candle", "value"),
State("impulse-vol-accel", "value"),
State("processing-ops-store", "data"),
prevent_initial_call=True
)
def run_grid_search_and_poll(n_clicks, poll_intervals, task_id, range_mult, vol_mult, body_ratio, wick_ratio, next_confirm, rsi_div, rsi_extreme, base_candle, vol_accel, processing_ops):
    triggered = ctx.triggered_id
    
    # 1. Handle Button Click (Start Grid Search)
    if triggered == "run-grid-search":
        if not task_id:
            return html.Div([html.H5("⚠️ Select a task first.", style={"color":"red"})]), processing_ops
            
        op_key = f"grid_{task_id}"
        if processing_ops.get(op_key):
            return html.Div([html.H5("⏳ Already running for this task...")]), processing_ops
            
        # Prepare params & data
        task = tm.get_task(task_id)
        if not task:
            return html.Div([html.H5("❌ Task not found.", style={"color":"red"})]), processing_ops
            
        param_grid = {'range_mult': [0.7, 1.0, 1.3], 'vol_mult': [1.2, 1.5], 'body_ratio': [0.4, 0.5], 'wick_ratio': [0.3, 0.4], 'use_next_candle_confirmation': [True, False], 'use_rsi_divergence': [False], 'use_base_candle': [False], 'use_volume_acceleration': [False]}
        processing_ops[op_key] = True
        from impulse import grid_search  # ✅ ADD THIS LINE
        try:
            fp = os.path.join(symbol_timeframe_path(task.symbols[0], task.timeframe), "data.parquet")
            # 🔧 CRITICAL: Clear cache before loading to ensure fresh data after recalc
            clear_parquet_cache()
            full_df = load_task_data_cached(task)
            buffer_ms = SIGNAL_BUFFER_MINUTES * 60 * 1000
            start_ms = max(0, task.signal_time - buffer_ms)
            if task.start_date and task.end_date:
                window_len_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) - int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
                df_limited = full_df[(full_df['timestamp'] >= start_ms) & (full_df['timestamp'] <= task.signal_time + window_len_ms)].copy()
            else:
                df_limited = full_df[full_df['timestamp'] >= start_ms].copy()
                
            if df_limited.empty:
                processing_ops.pop(op_key, None)
                return html.Div([html.H5("❌ No data in period.", style={"color":"red"})]), processing_ops
                
            job_id = f"grid_{task_id}"
            optimizer_mgr.submit(job_id, grid_search, df_limited, task.signal_price, task.signal_direction, task.signal_time, param_grid, verbose=False)
            
            return html.Div([
                html.H5("⏳ Grid Search Running: Testing 96 combinations..."),
                dcc.Interval(id={"type": "grid-poll", "index": task_id}, interval=1000, max_intervals=300),
                dcc.Store(id={"type": "grid-job-id", "index": task_id}, data=job_id)
            ]), processing_ops
        except Exception as e:
            processing_ops.pop(op_key, None)
            return html.Div([html.H5(f"❌ Error starting search: {e}", style={"color":"red"})]), processing_ops

    # 2. Handle Polling Interval
    if isinstance(triggered, dict) and triggered.get("type") == "grid-poll":
        task_id = triggered.get("index")
        job_id = f"grid_{task_id}"
        status = optimizer_mgr.get_status(job_id)
        
        if status['status'] == 'running':
            return html.Div([html.H5("⏳ Grid Search Running...")]), processing_ops
        if status['status'] == 'error':
            processing_ops.pop(job_id, None)
            return html.Div([html.H5(f"❌ Grid search failed: {status['error']}", style={"color":"red"})]), processing_ops
            
        processing_ops.pop(job_id, None)
        results_df = status['result']
        if results_df is None or results_df.empty:
            return html.Div([html.H5("⚠️ No impulse trades found in any combination.", style={"color":"orange"})]), processing_ops
            
        results_df = results_df.sort_values('total_pnl', ascending=False).head(5)
        table_rows = [html.Tr([
            html.Td(f"{r['range_mult']:.1f}"), html.Td(f"{r['vol_mult']:.1f}"), html.Td(f"{r['body_ratio']:.2f}"), html.Td(f"{r['wick_ratio']:.2f}"),
            html.Td("✓" if r['use_next_candle_confirmation'] else "✗"), html.Td("✓" if r['use_rsi_divergence'] else "✗"),
            html.Td("✓" if r['use_base_candle'] else "✗"), html.Td("✓" if r['use_volume_acceleration'] else "✗"),
            html.Td(f"{r['count']}"), html.Td(f"{r['win_rate']:.1f}%"), html.Td(f"{r['total_pnl']:.2f}%"), html.Td(f"{r['profit_factor']:.2f}"),
        ]) for _, r in results_df.iterrows()]
        
        return html.Div([
            html.H5("✅ Grid Search Complete (Top 5 Results)"),
            html.Table([html.Thead(html.Tr([html.Th("Range"), html.Th("Vol"), html.Th("Body"), html.Th("Wick"), html.Th("Next"), html.Th("Div"), html.Th("Base"), html.Th("Accel"), html.Th("Trades"), html.Th("Win%"), html.Th("P&L%"), html.Th("PF")])), html.Tbody(table_rows)], style={"width": "100%", "border": "1px solid gray", "borderCollapse": "collapse", "fontSize": "12px"})
        ]), processing_ops
        
    return no_update, processing_ops



@app.callback(
    Output("impulse-results", "children", allow_duplicate=True),
    Output("processing-ops-store", "data", allow_duplicate=True),
    Input({"type": "grid-poll", "index": ALL}, "n_intervals"),
    State("processing-ops-store", "data"),
    prevent_initial_call=True
)
def poll_grid_result(n_intervals, processing_ops):
    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update, processing_ops
    task_id = triggered.get("index")
    job_id = f"grid_{task_id}"
    status = optimizer_mgr.get_status(job_id)
    if status['status'] == 'running':
        return html.Div([html.H5("⏳ Grid search running...")]), processing_ops
    if status['status'] == 'error':
        processing_ops.pop(job_id, None)
        return html.Div([html.H5("❌ Grid search failed", style={"color":"red"}), html.Pre(status['error'])]), processing_ops
    processing_ops.pop(job_id, None)
    results_df = status['result']
    if results_df is None or results_df.empty:
        return html.Div([html.H5("⚠️ No impulse trades found", style={"color":"orange"})]), processing_ops
    results_df = results_df.sort_values('total_pnl', ascending=False).head(5)
    table_rows = [html.Tr([
        html.Td(f"{r['range_mult']:.1f}"), html.Td(f"{r['vol_mult']:.1f}"),
        html.Td(f"{r['body_ratio']:.2f}"), html.Td(f"{r['wick_ratio']:.2f}"),
        html.Td("✓" if r['use_next_candle_confirmation'] else "✗"),
        html.Td("✓" if r['use_rsi_divergence'] else "✗"),
        html.Td("✓" if r['use_base_candle'] else "✗"),
        html.Td("✓" if r['use_volume_acceleration'] else "✗"),
        html.Td(f"{r['count']}"), html.Td(f"{r['win_rate']:.1f}%"),
        html.Td(f"{r['total_pnl']:.2f}%"), html.Td(f"{r['profit_factor']:.2f}"),
    ]) for _, r in results_df.iterrows()]
    table = html.Table([
        html.Thead(html.Tr([html.Th("Range"), html.Th("Vol"), html.Th("Body"), html.Th("Wick"),
                            html.Th("Next"), html.Th("Div"), html.Th("Base"), html.Th("Accel"),
                            html.Th("Trades"), html.Th("Win%"), html.Th("Total P&L%"), html.Th("PF")])),
        html.Tbody(table_rows)
    ], style={"width": "100%", "border": "1px solid gray", "borderCollapse": "collapse", "fontSize": "12px"})
    return html.Div([html.H5("Grid Search Results (Top 5)"), table]), processing_ops



@app.callback(
    Output("impulse-results", "children", allow_duplicate=True),
    Input("run-walk-forward", "n_clicks"),
    State("impulse-task-selector", "value"),
    State("impulse-range-mult", "value"),
    State("impulse-vol-mult", "value"),
    State("impulse-body-ratio", "value"),
    State("impulse-wick-ratio", "value"),
    State("impulse-next-confirm", "value"),
    State("impulse-rsi-divergence", "value"),
    State("impulse-rsi-extreme", "value"),
    State("impulse-base-candle", "value"),
    State("impulse-vol-accel", "value"),
    prevent_initial_call=True
)
def run_walk_forward(n_clicks, task_id, range_mult, vol_mult, body_ratio, wick_ratio,
                     next_confirm, rsi_div, rsi_extreme, base_candle, vol_accel):
    if n_clicks == 0 or not task_id:
        return "Select a task and click Run Walk‑Forward."
    task = tm.get_task(task_id)
    if not task:
        return "Task not found."
    # WIDER, LOWER param grid to find impulses
    param_grid = {
        'range_mult': [0.5, 0.7, 0.9, 1.2],
        'vol_mult': [1.0, 1.2, 1.5],
        'body_ratio': [0.4, 0.5, 0.6],
        'wick_ratio': [0.3, 0.4, 0.5],
        'use_next_candle_confirmation': [True, False],
        'use_rsi_divergence': [True, False],
        'use_base_candle': [True, False],
        'use_volume_acceleration': [True, False],
    }
    try:
        from impulse import walk_forward
        # Load data (same as in apply_impulse_params)
        # 🔧 CRITICAL: Clear cache before loading to ensure fresh data after recalc
        clear_parquet_cache()
        full_df = load_task_data_cached(task)
        if full_df.empty:
            return "Data file not found or empty."
        buffer_ms = SIGNAL_BUFFER_MINUTES * 60 * 1000
        start_ms = max(0, task.signal_time - buffer_ms)
        if task.start_date and task.end_date:
            window_len_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) - int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
            cutoff_time = task.signal_time + window_len_ms
            df_limited = full_df[(full_df['timestamp'] >= start_ms) & (full_df['timestamp'] <= cutoff_time)].copy()
        else:
            df_limited = full_df[full_df['timestamp'] >= start_ms].copy()
        if df_limited.empty:
            return "No data in the selected period."
        # Run walk‑forward (percentage split works for any data length)
        results_df = walk_forward(df_limited, task.signal_price, task.signal_direction, task.signal_time,
                                  in_sample_pct=0.7, out_sample_pct=0.3, param_grid=param_grid, verbose=False)
        if results_df.empty:
            return "No walk‑forward results (insufficient data)."
        # Format the results as a table with readable timestamps
        table_rows = []
        for _, row in results_df.iterrows():
            in_range = f"{pd.to_datetime(row['in_start'], unit='ms').strftime('%Y-%m-%d %H:%M')} to {pd.to_datetime(row['in_end'], unit='ms').strftime('%Y-%m-%d %H:%M')}"
            out_range = f"{pd.to_datetime(row['out_start'], unit='ms').strftime('%Y-%m-%d %H:%M')} to {pd.to_datetime(row['out_end'], unit='ms').strftime('%Y-%m-%d %H:%M')}"
            params_str = ", ".join([f"{k}={v}" for k, v in row['best_params'].items()])
            table_rows.append(html.Tr([
                html.Td(in_range),
                html.Td(out_range),
                html.Td(params_str, style={"maxWidth": "200px", "fontSize": "11px"}),
                html.Td(f"{row['out_trades']}"),
                html.Td(f"{row['out_win_rate']:.1f}%"),
                html.Td(f"{row['out_total_pnl']:.2f}%"),
            ]))
        table = html.Table([
            html.Thead(html.Tr([
                html.Th("In‑Sample Range"), html.Th("Out‑Sample Range"),
                html.Th("Best Params"), html.Th("Trades"), html.Th("Win%"), html.Th("Total P&L%")
            ])),
            html.Tbody(table_rows)
        ], style={"width": "100%", "border": "1px solid gray", "borderCollapse": "collapse", "fontSize": "12px"})
        return html.Div([html.H5("Walk‑Forward Results (70% train, 30% test)"), table])
    except Exception as e:
        return f"Walk‑forward error: {str(e)}"



@app.callback(
    Input({"type": "rerun-strat-btn", "index": ALL}, "n_clicks"),
    prevent_initial_call=True
)
def rerun_strategy(n_clicks_list):
    # FIX: Stop phantom triggers caused by table re-rendering (resetting n_clicks to None/0)
    if not any(n_clicks_list):
        return no_update

    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update
    task_id = triggered.get("index")
    task = tm.get_task(task_id)
    if not task or task.status != "completed":
        return no_update
    try:
        # Reload data and re-run detect_strategies
        sym = task.symbols[0]
        path = symbol_timeframe_path(sym, task.timeframe)
        fp = os.path.join(path, "data.parquet")
        if not os.path.exists(fp):
            task.add_log("Re‑run Strategy: data file not found")
            return no_update
        full_df = pd.read_parquet(fp)
        buffer_ms = SIGNAL_BUFFER_MINUTES * 60 * 1000
        start_ms = max(0, task.signal_time - buffer_ms)
        if task.start_date and task.end_date:
            window_len_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) - int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
            cutoff_time = task.signal_time + window_len_ms
            df_limited = full_df[(full_df['timestamp'] >= start_ms) & (full_df['timestamp'] <= cutoff_time)].copy()
        else:
            df_limited = full_df[full_df['timestamp'] >= start_ms].copy()
        if df_limited.empty:
            task.add_log("Re‑run Strategy: no data after filtering")
            return no_update
        signals = detect_strategies(df_limited, task.signal_price, task.signal_direction, task.signal_time, verbose=False)
        # Replace all signals
        task.strategy_signals = []
        for sig in signals:
            task.add_strategy_signal(
                sig['type'], sig['direction'], sig['entry_price'], sig['entry_time_ms'],
                exit_price=sig.get('exit_price'), exit_time_ms=sig.get('exit_time_ms'),
                stop_loss=sig.get('stop_loss'), take_profit=sig.get('take_profit_1'),
                confidence=sig['confidence']
            )
        # Update best summary
        if task.strategy_signals:
            best = max(task.strategy_signals, key=lambda x: x['delta_pct'] if x.get('delta_pct') is not None else -999)
            task.strategy_log_summary = f"{best['type'].capitalize()} {best['direction'].upper()} ({best.get('delta_pct', 0):.1f}%)"
            task.strategy_confidence = best['confidence']
        else:
            task.strategy_log_summary = "No valid signal"
        task.add_log("Manual strategy re‑run completed")
        return no_update
    except Exception as e:
        task.add_log(f"Manual strategy re‑run error: {e}")
        return no_update



@app.callback(
    Output("impulse-apply-all-status", "children", allow_duplicate=True),
    Input("rerun-strat-all", "n_clicks"),
    State("impulse-range-mult", "value"),
    State("impulse-vol-mult", "value"),
    State("impulse-body-ratio", "value"),
    State("impulse-wick-ratio", "value"),
    State("impulse-next-confirm", "value"),
    State("impulse-rsi-divergence", "value"),
    State("impulse-rsi-extreme", "value"),
    State("impulse-base-candle", "value"),
    State("impulse-vol-accel", "value"),
    prevent_initial_call=True
)
def rerun_strategy_on_all(n_clicks, range_mult, vol_mult, body_ratio, wick_ratio,
                          next_confirm, rsi_div, rsi_extreme, base_candle, vol_accel):
    if n_clicks == 0:
        return ""
    tasks = tm.get_all_tasks()
    completed = [t for t in tasks if t.status == "completed"]
    if not completed:
        return "No completed tasks."
    success = 0
    for task in completed:
        try:
            # Re‑load data (same logic as in rerun_strategy)
            sym = task.symbols[0]
            path = symbol_timeframe_path(sym, task.timeframe)
            fp = os.path.join(path, "data.parquet")
            if not os.path.exists(fp):
                continue
            full_df = pd.read_parquet(fp)
            buffer_ms = task.pre_buffer_minutes * 60 * 1000
            start_ms = max(0, task.signal_time - buffer_ms)
            if task.start_date and task.end_date:
                window_len_ms = int(task.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) - int(task.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
                cutoff_time = task.signal_time + window_len_ms
                df_limited = full_df[(full_df['timestamp'] >= start_ms) & (full_df['timestamp'] <= cutoff_time)].copy()
            else:
                df_limited = full_df[full_df['timestamp'] >= start_ms].copy()
            if df_limited.empty:
                continue
            signals = detect_strategies(df_limited, task.signal_price, task.signal_direction, task.signal_time, verbose=False)
            task.strategy_signals = []
            for sig in signals:
                task.add_strategy_signal(
                    sig['type'], sig['direction'], sig['entry_price'], sig['entry_time_ms'],
                    exit_price=sig.get('exit_price'), exit_time_ms=sig.get('exit_time_ms'),
                    stop_loss=sig.get('stop_loss'), take_profit=sig.get('take_profit_1'),
                    confidence=sig['confidence']
                )
            # Update best summary
            if task.strategy_signals:
                best = max(task.strategy_signals, key=lambda x: x['delta_pct'] if x.get('delta_pct') is not None else -999)
                task.strategy_log_summary = f"{best['type'].capitalize()} {best['direction'].upper()} ({best.get('delta_pct', 0):.1f}%)"
                task.strategy_confidence = best['confidence']
            else:
                task.strategy_log_summary = "No valid signal"
            success += 1
        except Exception as e:
            task.add_log(f"Re‑run Strategy on All error: {e}")
    return f"Re‑run Strategy completed on {success} tasks."



@app.callback(
    Output("impulse-apply-all-status", "children", allow_duplicate=True),
    Input("rerun-impulse-all", "n_clicks"),
    State("impulse-range-mult", "value"),
    State("impulse-vol-mult", "value"),
    State("impulse-body-ratio", "value"),
    State("impulse-wick-ratio", "value"),
    State("impulse-next-confirm", "value"),
    State("impulse-rsi-divergence", "value"),
    State("impulse-rsi-extreme", "value"),
    State("impulse-base-candle", "value"),
    State("impulse-vol-accel", "value"),
    prevent_initial_call=True
)
def rerun_impulse_on_all(n_clicks, range_mult, vol_mult, body_ratio, wick_ratio,
                         next_confirm, rsi_div, rsi_extreme, base_candle, vol_accel):
    if n_clicks == 0:
        return ""
    tasks = tm.get_all_tasks()
    completed = [t for t in tasks if t.status == "completed"]
    if not completed:
        return "No completed tasks."
    params = {
        'range_mult': range_mult,
        'vol_mult': vol_mult,
        'body_ratio': body_ratio,
        'wick_ratio': wick_ratio,
        'use_next_candle_confirmation': 'confirm' in next_confirm if next_confirm else False,
        'use_rsi_divergence': 'div' in rsi_div if rsi_div else False,
        'rsi_extreme': rsi_extreme,
        'use_base_candle': 'base' in base_candle if base_candle else False,
        'use_volume_acceleration': 'accel' in vol_accel if vol_accel else False,
    }
    success = 0
    total_impulse = 0
    for task in completed:
        try:
            cnt = task.run_impulse_detection(params=params, verbose=False)
            total_impulse += cnt
            success += 1
        except Exception as e:
            task.add_log(f"Re‑run Impulse on All error: {e}")
    return f"Re‑run Impulse completed on {success} tasks. Total impulse signals: {total_impulse}"

# ----- Database Maintenance Callbacks -----


@app.callback(
    Output("clean-symbol", "options"),
    Input("main-tabs", "value")
)
def update_clean_symbols(tab):
    if tab != "tab-analysis":
        return []
    info = get_database_info()
    symbols = sorted(set(d["symbol"] for d in info["details"]))
    return [{"label": s, "value": s} for s in symbols]



@app.callback(
    Output("clean-timeframe", "options"),
    Input("clean-symbol", "value")
)
def update_clean_timeframes(symbol):
    if not symbol:
        return []
    info = get_database_info()
    timeframes = sorted(set(d["timeframe"] for d in info["details"] if d["symbol"] == symbol))
    return [{"label": tf, "value": tf} for tf in timeframes]



@app.callback(
    Output("delete-status", "children"),
    Input("delete-selected-btn", "n_clicks"),
    State("clean-symbol", "value"),
    State("clean-timeframe", "value"),
    prevent_initial_call=True
)
def delete_selected_data(n_clicks, symbol, timeframe):
    if not symbol or not timeframe:
        return "❌ Please select both symbol and timeframe."
    path = symbol_timeframe_path(symbol, timeframe)
    fp = os.path.join(path, "data.parquet")
    if not os.path.exists(fp):
        return f"⚠️ Data file not found for {symbol} {timeframe}."
    try:
        os.remove(fp)
        if os.path.exists(path) and not os.listdir(path):
            os.rmdir(path)
        return f"✅ Deleted {symbol} {timeframe} data. You can now re‑run tasks with 'Overwrite' checked."
    except Exception as e:
        return f"❌ Error deleting: {str(e)}"



@app.callback(
    Output("delete-all-btn", "disabled"),
    Input("confirm-delete-all", "value")
)
def enable_delete_all(confirm):
    return "confirm" not in confirm



@app.callback(
    Output("delete-status", "children", allow_duplicate=True),
    Input("delete-all-btn", "n_clicks"),
    prevent_initial_call=True
)
def delete_all_data(n_clicks):
    if n_clicks is None:
        return ""
    try:
        import shutil
        shutil.rmtree(MARKET_DATA_DIR)
        os.makedirs(MARKET_DATA_DIR, exist_ok=True)
        return "✅ All market data deleted. You can now re‑run tasks to download fresh data."
    except Exception as e:
        return f"❌ Error deleting all data: {str(e)}"



@app.callback(
    Output("delete-status", "children", allow_duplicate=True),
    Input("redownload-full-btn", "n_clicks"),
    State("clean-symbol", "value"),
    State("clean-timeframe", "value"),
    prevent_initial_call=True
)
def redownload_full_history(n_clicks, symbol, timeframe):
    if not symbol or not timeframe:
        return "❌ Please select both symbol and timeframe."
    # Delete existing file first
    path = symbol_timeframe_path(symbol, timeframe)
    fp = os.path.join(path, "data.parquet")
    if os.path.exists(fp):
        os.remove(fp)
    if os.path.exists(path) and not os.listdir(path):
        os.rmdir(path)
    # Create a task with mode='full'
    import uuid
    import time
    tid = str(uuid.uuid4())
    fake_signal_time = int(time.time() * 1000)
    task = DownloadTask(
        task_id=tid,
        symbols=[symbol],
        timeframe=timeframe,
        mode='full',
        start_date=None,
        end_date=None,
        overwrite=True,
        price_continuity_check=False,
        signal_time=fake_signal_time,
        signal_price=0,
        signal_symbol=symbol,
        signal_direction='resistance',
        analyze_beyond=False,
        enable_strategy=False,
        enable_impulse=False,
        pre_buffer_minutes=5
    )
    tm.add_task(task)
    task.add_log(f"Re‑download full history for {symbol} {timeframe}")
    return f"🔄 Started re‑download of full history for {symbol} {timeframe}. Watch the Tasks tab for progress."

# ----- Active Download Monitor Callbacks -----


@app.callback(
    Output("monitor-task-info", "children"),
    Output("monitor-progress", "value"),
    Output("monitor-pause-btn", "disabled"),
    Output("monitor-stop-btn", "disabled"),
    Input("progress-interval", "n_intervals"),
    prevent_initial_call=True
)
def update_download_monitor(_):
    running = [t for t in tm.get_all_tasks() if t.status == "running"]
    if not running:
        return "Idle", "0", True, True
    task = running[0]
    sym = task.symbols[0]
    info = f"{sym} | {task.timeframe} | {task.downloaded_candles}/{task.total_candles} candles"
    return info, str(int(task.progress)), False, False



@app.callback(
    Output("monitor-pause-btn", "children", allow_duplicate=True),
    Input("monitor-pause-btn", "n_clicks"),
    prevent_initial_call=True
)
def monitor_pause(n_clicks):
    if n_clicks is None:
        return "⏸ Pause"
    running = [t for t in tm.get_all_tasks() if t.status == "running"]
    if not running:
        return "⏸ Pause"
    task = running[0]
    tm.pause_task(task.task_id)
    return "▶ Resume" if task.paused else "⏸ Pause"



@app.callback(
    Output("monitor-stop-btn", "n_clicks", allow_duplicate=True),
    Input("monitor-stop-btn", "n_clicks"),
    prevent_initial_call=True
)
def monitor_stop(n_clicks):
    if n_clicks is None:
        return 0
    running = [t for t in tm.get_all_tasks() if t.status == "running"]
    if running:
        tm.stop_task(running[0].task_id)
    return 0

# ----- Re-download ALL Existing Data Callback -----


@app.callback(
    Output("redownload-all-status", "children"),
    Input("redownload-all-btn", "n_clicks"),
    prevent_initial_call=True
)
def redownload_all_existing(n_clicks):
    if n_clicks is None:
        return ""
    try:
        pairs = []
        for root, _, files in os.walk(MARKET_DATA_DIR):
            if "data.parquet" in files:
                rel = os.path.relpath(root, MARKET_DATA_DIR).split(os.sep)
                if len(rel) == 2:
                    sym, tf = rel
                    pairs.append((sym, tf))
        if not pairs:
            return "⚠️ No existing data found to re-download."
        queued = 0
        for sym, tf in pairs:
            path = symbol_timeframe_path(sym, tf)
            fp = os.path.join(path, "data.parquet")
            if os.path.exists(fp):
                os.remove(fp)
            tid = str(uuid.uuid4())
            task = DownloadTask(
                task_id=tid, symbols=[sym], timeframe=tf, mode='full',
                start_date=None, end_date=None, overwrite=True,
                price_continuity_check=False, signal_time=int(time.time()*1000),
                signal_price=0, signal_symbol=sym, signal_direction='resistance',
                analyze_beyond=False, enable_strategy=False, enable_impulse=False,
                pre_buffer_minutes=5
            )
            tm.add_task(task)
            queued += 1
            # Add immediate log so UI picks it up on next interval refresh
            task.add_log(f"🔄 Full history re-download queued for {sym} ({tf})")
        return f"✅ Queued {queued} full re-download tasks. Progress will appear in Tasks tab shortly."
    except Exception as e:
        return f"❌ Error: {str(e)}"



@app.callback(
    Output("bulk-rerun-status", "children"),
    Input("bulk-rerun-events", "n_clicks"),
    Input("bulk-rerun-strategy", "n_clicks"),
    Input("bulk-rerun-impulse", "n_clicks"),
    prevent_initial_call=True
)
def bulk_rerun_all(ev_n, str_n, imp_n):
    triggered = ctx.triggered_id
    if not triggered:
        return "Ready"
    
    tasks = tm.get_all_tasks()
    completed = [t for t in tasks if t.status == "completed"]
    
    if not completed:
        return "⚠️ No completed tasks found to re-run."
        
    count = 0
    for t in completed:
        try:
            if triggered == "bulk-rerun-events":
                # Runs analyze_signal() which generates all detailed logs you need
                t.analyze_signal()
                
            elif triggered == "bulk-rerun-strategy":
                sym = t.symbols[0]
                path = symbol_timeframe_path(sym, t.timeframe)
                fp = os.path.join(path, "data.parquet")
                if os.path.exists(fp):
                    full_df = pd.read_parquet(fp)
                    buffer_ms = t.pre_buffer_minutes * 60 * 1000
                    start_ms = max(0, t.signal_time - buffer_ms)
                    
                    if t.start_date and t.end_date:
                        window_len_ms = int(t.end_date.replace(tzinfo=timezone.utc).timestamp() * 1000) - int(t.start_date.replace(tzinfo=timezone.utc).timestamp() * 1000)
                        cutoff_time = t.signal_time + window_len_ms
                        df_limited = full_df[(full_df['timestamp'] >= start_ms) & (full_df['timestamp'] <= cutoff_time)].copy()
                    else:
                        df_limited = full_df[full_df['timestamp'] >= start_ms].copy()
                        
                    if not df_limited.empty:
                        signals = detect_strategies(df_limited, t.signal_price, t.signal_direction, t.signal_time, verbose=False)
                        t.strategy_signals = []
                        for sig in signals:
                            t.add_strategy_signal(
                                sig['type'], sig['direction'], sig['entry_price'], sig['entry_time_ms'],
                                exit_price=sig.get('exit_price'),
                                exit_time_ms=sig.get('exit_time_ms'),
                                stop_loss=sig.get('stop_loss'),
                                take_profit=sig.get('take_profit_1'),
                                confidence=sig['confidence']
                            )
                    
                    if t.strategy_signals:
                        best = max(t.strategy_signals, key=lambda x: x.get('delta_pct') if x.get('delta_pct') is not None else -999)
                        dp = best.get('delta_pct')
                        dp_val = dp if dp is not None else 0.0
                        t.strategy_log_summary = f"{best['type'].capitalize()} {best['direction'].upper()} ({dp_val:.1f}%)"
                        t.strategy_confidence = best['confidence']
                        
            elif triggered == "bulk-rerun-impulse":
                t.run_impulse_detection(verbose=False)
            count += 1
        except Exception as e:
            t.add_log(f"Bulk rerun error: {e}")
            
    label = "Events" if triggered == "bulk-rerun-events" else "Strategy" if triggered == "bulk-rerun-strategy" else "Impulse"
    return f"✅ {label} re-run completed on {count} tasks. Table will refresh shortly."

# 1. Auto-refresh dropdown with existing JSON files


@app.callback(
    Output("json-file-select", "options"),
    Input("save-tasks-btn", "n_clicks"),
    Input("load-tasks-btn", "n_clicks"),
    prevent_initial_call=True
)
def refresh_json_dropdown(*_):
    if not os.path.exists(LOGS_DIR):
        return []
    files = sorted([f for f in os.listdir(LOGS_DIR) if f.endswith('.json')], reverse=True)
    return [{"label": f, "value": os.path.join(LOGS_DIR, f)} for f in files]

# 2. Save tasks to custom JSON filename (REWRITTEN: Reconstruction from Truth pattern)


@app.callback(
    Output("save-load-status", "children", allow_duplicate=True),
    Output("task-ids-store", "data", allow_duplicate=True),
    Output("task-count-store", "data", allow_duplicate=True),
    Output("task-page-store", "data", allow_duplicate=True),
    Input("clear-all-tasks-btn", "n_clicks"),
    prevent_initial_call=True
)
def manual_clear_all(n):
    """Instantly wipes all tasks from RAM and resets UI stores."""
    global STOP_REQUESTED
    STOP_REQUESTED = True  # 🔧 Safely halt background recalc (sync with STOP_REQUESTED)
    recalc_bg["stop_flag"] = True  # 🔧 Also set recalc_bg flag for UI
    with tm.lock:
        tm.tasks.clear()
    return "🗑️ All tasks cleared.", [], 0, 0



@app.callback(
    Output("bulk-rerun-status", "children", allow_duplicate=True),
    Output("analysis-complete-trigger", "data", allow_duplicate=True), # 🔧 NEW
    Input("recalc-table-flags-btn", "n_clicks"),
    prevent_initial_call=True
)
def recalc_table_flags(n):
    """Recomputes ONLY the table column flags..."""
    global STOP_REQUESTED
    if not n: 
        return dash.no_update, dash.no_update  # 🔧 Return tuple
    
    # 🔧 CRITICAL: Reset stop flag before starting new recalculation
    STOP_REQUESTED = False
    
    if recalc_bg["running"]: 
        return "⏳ Recalculation already in progress...", dash.no_update  # 🔧 Return tuple
        
    tasks = [t for t in tm.get_all_tasks() if t.signal_time is not None and t.status == "completed"]
    if not tasks:
        return "⚠️ No completed tasks with signal data to recalc.", dash.no_update  # 🔧 Return tuple

    # 🔧 CRITICAL: Serialize tasks to dict format INSIDE the main thread (same logic as save_tasks_to_json)
    # This ensures all attributes are properly captured before passing to background thread
    import copy
    initial_tasks = []
    for t in tasks:
        d = {}
        for k, v in t.__dict__.items():
            # Skip non-serializable objects (locks, events, caches)
            if k in ('stop_event', 'pause_event', 'state_lock', 'raw_batches', '_chart_cache', 'symbol_ranges'):
                continue
            # Handle datetime objects
            if isinstance(v, (datetime, pd.Timestamp)):
                d[k] = v.isoformat()
            elif isinstance(v, (int, float, str, bool, type(None))):
                d[k] = v
            elif isinstance(v, (list, dict)):
                try:
                    json.dumps(v)
                    d[k] = v
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    d[k] = str(v)
                except Exception:
                    continue
        initial_tasks.append(d)
    
    # 🔧 CRITICAL: Set global counters
    global recalc_total_tasks, is_recalculating_flag, recalc_progress_count
    recalc_total_tasks = len(initial_tasks)
    is_recalculating_flag = True
    recalc_progress_count = 0
    
    # 🔧 CRITICAL: Update recalc_bg status BEFORE starting thread
    recalc_bg["running"] = True
    recalc_bg["total"] = len(initial_tasks)
    recalc_bg["count"] = 0
    recalc_bg["stop_flag"] = False  # 🔧 Reset stop flag in recalc_bg dict
    recalc_bg["trigger_val"] = 0  # 🔧 Reset trigger value
    
    # 🔧 CRITICAL: Enable the poller to monitor completion
    global recalc_poller_enabled
    recalc_poller_enabled = True
    
    # 🔧 CRITICAL: Start background thread passing initial_tasks as argument
    import threading
    threading.Thread(target=_run_recalc_background, args=(initial_tasks,), daemon=True).start()

    # 🔧 Increment trigger to force UI refresh after recalc starts
    import time
    trigger_val = int(time.time())

    return f"🔄 Recalculation started in background. Checking {len(tasks)} existing tasks...", trigger_val  # 🔧 Already correct

def _run_recalc_background(tasks_list):
    """Runs in background thread to never block the UI."""
    global recalc_progress_count, is_recalculating_flag, recalculation_complete_timestamp, current_tasks, STOP_REQUESTED, recalc_bg
    
    # 🔧 CRITICAL: Create LOCAL ALIASES for modules to avoid global lookup issues in threads
    import sys as _sys
    import bisect as _bisect
    import numpy as np
    import pandas as pd
    
    # Create module-level aliases accessible throughout this function
    sys = _sys
    bisect = _bisect
    
    # 🔧 CRITICAL: DO NOT clear parquet cache - we use cached data from RAM for fast analysis
    # The original design was to avoid re-reading files when analyzing JSON-loaded tasks
    
    # 🔧 HEARTBEAT: Confirm thread started
    print(f"🔥 [RECALC THREAD] Started with {len(tasks_list)} tasks")
    sys.stdout.flush()
    
    total_tasks = len(tasks_list)
    
    # 🔧 DYNAMIC STEP CALCULATOR: Ensures ~50 progress updates regardless of batch size
    # For 10 tasks: step = max(1, 10//50) = 1 → updates every task (10 updates)
    # For 89 tasks: step = max(1, 89//50) = 1 → updates every task (89 updates)
    # For 3500 tasks: step = max(1, 3500//50) = 70 → updates every 70 tasks (50 updates)
    step = max(1, total_tasks // 50)
    print(f"🔥 [RECALC THREAD] Dynamic step calculated: {step} (total={total_tasks})")
    sys.stdout.flush()
    
    # 🔧 DATETIME FIELDS that need restoration from ISO strings
    datetime_fields = {'start_date', 'end_date', 'first_event_time', 'max_adverse_time',
                       'max_expected_time', 'max_adverse_sgnl_time', 'max_expected_sgnl_time',
                       'max_adverse_before_return_time', 'max_adverse_before_return_sgnl_time',
                       'drawdown_before_level_time', 'drawdown_before_1pct_time', 
                       'drawdown_before_1_5pct_time', 'drawdown_before_2pct_time'}
    
    # 🔧 Use global _parse_timestamp for UTC-aware datetime parsing
    # (Defined at module level for consistency across save/load operations)
    
    # 🔧 TRACK SUCCESS/FAILURE COUNTS
    success_count = 0
    error_count = 0
    
    for i, t_dict in enumerate(tasks_list):
        # 🛑 PATCH A: Check for stop request every iteration (check both flags)
        if STOP_REQUESTED or recalc_bg.get("stop_flag", False):
            print(f"⚠️ [RECALC THREAD] Stop requested at {i}/{total_tasks}. Finishing safely...")
            sys.stdout.flush()
            break
            
        try:
            # 🔧 RECONSTRUCT TASK OBJECT FROM DICTIONARY
            # Get task from memory if it exists, otherwise create a new one from dict
            task_id = t_dict.get('task_id')
            task_symbol = t_dict.get('symbols', ['UNKNOWN'])[0] if isinstance(t_dict.get('symbols'), list) else 'UNKNOWN'
            task_tf = t_dict.get('timeframe', 'unknown')
            
            print(f"🔍 [TASK {i+1}/{total_tasks}] Starting: {task_symbol} {task_tf} (ID: {task_id})")
            sys.stdout.flush()
            
            task = tm.get_task(task_id) if task_id else None
            
            if task is None:
                # Reconstruct task from dictionary
                init_kwargs = {k: t_dict.get(k) for k in ['task_id', 'symbols', 'timeframe', 'mode', 'start_date', 'end_date',
                    'overwrite', 'price_continuity_check', 'signal_time', 'signal_price',
                    'signal_symbol', 'signal_direction', 'analyze_beyond', 'enable_strategy',
                    'enable_impulse', 'pre_buffer_minutes', 'log_events', 'hide_logs']}
                
                # Parse Datetimes
                for k in datetime_fields:
                    if k in init_kwargs and isinstance(init_kwargs[k], str):
                        init_kwargs[k] = _parse_timestamp(init_kwargs[k])
                
                task = DownloadTask(**init_kwargs)
                
                # Restore ALL Other Attributes from Dictionary
                for k, v in t_dict.items():
                    if hasattr(task, k) and k not in init_kwargs:
                        try:
                            if k in datetime_fields:
                                setattr(task, k, _parse_timestamp(v))
                            elif k in ['signal_time', 'signal_price']:
                                setattr(task, k, float(v))
                            else:
                                setattr(task, k, v)
                        except Exception:
                            pass
            
            # Now process the reconstructed task object
            if task.signal_time is not None and task.status == "completed":
                print(f"📊 [TASK {i+1}/{total_tasks}] Running analyze_signal for {task_symbol} {task_tf}...")
                sys.stdout.flush()
                
                # 🔧 CRITICAL: Acquire state_lock before modifying strategy signals
                with task.state_lock:
                    task.analyze_signal()  # This is the slow part
                    
                print(f"✅ [TASK {i+1}/{total_tasks}] Completed analyze_signal for {task_symbol} {task_tf}")
                sys.stdout.flush()
                
                # 🔧 CRITICAL: Auto-save recalculated tasks to persist new data
                task.add_log("💾 Recalculation complete - data updated in memory")
                success_count += 1  # ✅ Track successful recalculation
            else:
                print(f"⏭️ [TASK {i+1}/{total_tasks}] Skipping (no signal_time or not completed): {task_symbol} {task_tf}")
                sys.stdout.flush()
                # Skipped tasks don't count as errors or successes
        except Exception as e:
            # ⚠️ WARNING ONLY: Continue processing even if task has errors (old Mac safe)
            import traceback
            print(f"❌ [TASK {i+1}/{total_tasks}] ERROR on {task_symbol if 'task_symbol' in locals() else 'UNKNOWN'} {task_tf if 'task_tf' in locals() else 'unknown'}: {e}")
            traceback.print_exc()
            sys.stdout.flush()
            error_count += 1  # ❌ Track failed recalculation
            try: 
                if task:
                    task.add_log(f"⚠️ Recalc error: {e}")
            except: pass

        # 🔧 CRITICAL: Update progress counter with DYNAMIC STEP for any batch size
        # This prevents freezing where small task counts would never reach the update threshold
        if (i + 1) % step == 0 or (i + 1) == total_tasks:
            recalc_progress_count = i + 1
            recalc_bg["count"] = i + 1  # 🔧 Update recalc_bg for UI polling
            print(f"🔥 [RECALC THREAD] Progress: {i + 1}/{total_tasks} (step={step})")
            sys.stdout.flush()
            
        # 🔧 HEARTBEAT: Every 10 seconds, print a heartbeat to confirm thread is alive
        if (i + 1) % max(10, step) == 0:
            print(f"💓 [RECALC THREAD] Heartbeat: Processing task {i + 1}/{total_tasks}...")
            sys.stdout.flush()

    # 🔧 CRITICAL: Update global RAM with processed tasks (atomic swap)
    with tm.lock:
        # Tasks were modified in-place during the loop, so they're already in tm.tasks
        # Just ensure current_tasks reflects the latest state
        current_tasks = list(tm.tasks.values())
    
    # 🔧 GOLDEN STORE: Populate pre-processed cache for instant pagination
    global golden_task_store_data, golden_store_version
    with tm.lock:
        golden_task_store_data = list(tm.tasks.values())
        golden_store_version += 1  # Increment version to invalidate page caches

    # 🔧 RECALC LOCK: Release lock to allow UI interaction
    global recalc_lock
    recalc_lock = {"locked": False, "message": "Recalculation complete"}
    
    # 🔧 CRITICAL: Update flags and timestamp (NO Auto-Save - user must press Save button)
    recalculation_complete_timestamp = time.time()
    is_recalculating_flag = False
    STOP_REQUESTED = False  # Reset stop flag for next run
    final_count = i + 1 if STOP_REQUESTED else total_tasks
    recalc_progress_count = final_count
    recalc_bg["count"] = final_count  # 🔧 Final count update
    recalc_bg["running"] = False  # 🔧 Signal completion to UI
    recalc_bg["trigger_val"] = int(time.time() * 1000)  # 🔧 NEW: Store trigger value for polling
    
    # 🔧 CRITICAL: Increment trigger to force UI refresh AFTER recalculation completes
    # This ensures task table and summary table show the updated data
    analysis_trigger_val = int(time.time() * 1000)  # Use milliseconds to ensure unique value

    if STOP_REQUESTED:
        print(f"⚠️ [RECALC THREAD] Recalculation stopped early: {final_count}/{total_tasks} tasks processed")
    elif error_count > 0:
        # 🚨 HONEST REPORTING: Show errors prominently
        print(f"🔴 [RECALC THREAD] Recalculation completed with ERRORS: {success_count} succeeded, {error_count} failed out of {total_tasks} tasks. FIX ERRORS before saving!")
    elif success_count == 0:
        # 🚨 HONEST REPORTING: No tasks were actually recalculated
        print(f"🔴 [RECALC THREAD] Recalculation completed but NOTHING WAS UPDATED: 0/{total_tasks} tasks recalculated. Check task status and signal data!")
    else:
        # ✅ Calculate how many tasks were skipped (no signal_time or not completed)
        skipped_count = total_tasks - success_count - error_count
        if skipped_count > 0:
            print(f"✅ [RECALC THREAD] Recalculation successful: {success_count}/{total_tasks} tasks updated.")
            print(f"ℹ️ [RECALC THREAD] Note: {skipped_count} task(s) were skipped (no signal time or incomplete status).")
            print(f"💾 [RECALC THREAD] Results in RAM - press 'Save New JSON' to persist.")
        else:
            print(f"✅ [RECALC THREAD] Recalculation successful: {success_count}/{total_tasks} tasks updated. Results in RAM - press 'Save New JSON' to persist.")
    sys.stdout.flush()
    
    # 🔧 CRITICAL: Return the trigger value so callback can update the store
    return analysis_trigger_val




@app.callback(
    Output("recalc-status-bar", "children"),
    Input("recalc-status-interval", "n_intervals"),
    prevent_initial_call=False
)
def update_status_bar(n):
    """Real-time status bar callback triggered every 1 second."""
    if is_recalculating_flag:
        # 🔧 FIX: Use recalc_bg["count"] for real-time progress instead of recalc_progress_count
        # which only updates in batches and can appear frozen
        current_count = recalc_bg.get("count", 0) if recalc_bg.get("running", False) else recalc_progress_count
        return f"⚙️ Checking: {current_count} / {recalc_total_tasks} tasks..."
    else:
        return "Ready"



@app.callback(
    Output("bulk-rerun-status", "children", allow_duplicate=True),
    Output("analysis-complete-trigger", "data", allow_duplicate=True), # 🔧 NEW: Also update trigger when polling detects completion
    Input("progress-interval", "n_intervals"),
    prevent_initial_call=True
)
def poll_recalc_progress(_):
    if not recalc_bg["running"]:
        # 🔧 FIX: Return a completion message instead of no_update
        # This ensures the UI shows "Done" instead of getting stuck on the last progress count
        if recalc_bg["total"] > 0:
            # 🔧 CRITICAL: Check if we have a trigger value from completed recalculation
            trigger_val = recalc_bg.get("trigger_val", 0)
            if trigger_val > 0:
                return f"✅ Recalculation complete. ({recalc_bg['count']}/{recalc_bg['total']} tasks updated)", trigger_val
            return f"✅ Recalculation complete. ({recalc_bg['count']}/{recalc_bg['total']} tasks updated)", dash.no_update
        else:
            return no_update, dash.no_update
    return f"⏳ Recalculating... {recalc_bg['count']}/{recalc_bg['total']} completed", dash.no_update

# 🔧 NEW: Dedicated poller for triggering UI refresh after recalculation completes


@app.callback(
    Output("recalc-poller", "disabled"),
    Output("analysis-complete-trigger", "data", allow_duplicate=True),
    Input("recalc-poller", "n_intervals"),
    State("recalc-poller", "disabled"),
    prevent_initial_call=True
)
def trigger_ui_on_recalc_complete(n_intervals, is_disabled):
    """Polls every 1 second during recalculation and triggers UI refresh when complete."""
    global recalc_poller_enabled
    
    # Check if recalculation just finished
    if not recalc_bg["running"] and recalc_poller_enabled:
        # Recalculation just finished - trigger UI refresh
        trigger_val = recalc_bg.get("trigger_val", int(time.time() * 1000))
        print(f"🔥 [UI POLLER] Recalculation complete! Triggering UI refresh with value: {trigger_val}")
        # Reset poller state
        recalc_poller_enabled = False
        # Enable (disable=True) the poller until next recalculation
        return True, trigger_val
    elif recalc_bg["running"] and not recalc_poller_enabled:
        # Recalculation started - keep poller enabled (disabled=False)
        recalc_poller_enabled = True
        return False, dash.no_update
    # Keep current state
    return dash.no_update, dash.no_update

if __name__ == "__main__":
    app.run(debug=True, port=8050)
if __name__ == '__main__':
    app.run_server(debug=True, host='0.0.0.0', port=8050)
def create_signal_tasks(n_clicks, signals, period_type, start_date, end_date, hours, tf, ow, beyond_val, stored_ids, strat_val, imp_val, pre_buffer, event_log_val, hide_logs_val, autoclear_val, count):
    """Parses signals and creates tasks with background processing for large batches."""
    if not signals:
        return stored_ids, count
    
    ow_flag = "overwrite" in ow if ow else False
    analyze_beyond = "beyond" in beyond_val if beyond_val else False
    strat_disabled = "disable" in strat_val if strat_val else False
    imp_disabled = "disable" in imp_val if imp_val else False
    log_events = "disable" not in event_log_val if event_log_val else True
    
    # AUTO-CLEAR LOGIC: Wipe memory if checkbox is checked
    if autoclear_val and "autoclear" in autoclear_val:
        tm.tasks.clear()
        new_ids = []
        new_count = 0
    else:
        new_ids = stored_ids.copy() if stored_ids else []
        new_count = count
        
    buffer_ms = SIGNAL_BUFFER_MINUTES * 60 * 1000
    
    total_signals = len(signals)
    
    # 🔧 CRITICAL: For large batches (>100 signals), use background processing
    # This prevents UI freeze during parsing
    if total_signals > 100:
        print(f"🚀 Large batch detected ({total_signals} signals) - using background processing...")
        
        # 🔧 Prepare serialized data for background thread
        import copy
        parse_data = {
            'signals': signals,
            'period_type': period_type,
            'start_date': start_date,
            'end_date': end_date,
            'hours': hours,
            'tf': tf,
            'ow_flag': ow_flag,
            'analyze_beyond': analyze_beyond,
            'strat_disabled': strat_disabled,
            'imp_disabled': imp_disabled,
            'log_events': log_events,
            'hide_logs_val': hide_logs_val,
            'pre_buffer': pre_buffer,
            'existing_ids': list(new_ids),
            'existing_count': new_count
        }
        
        # 🔧 Start background thread
        import threading
        threading.Thread(target=_run_parse_background, args=(parse_data,), daemon=True).start()
        
        return f"🔄 Processing {total_signals} signals in background...", dash.no_update
    
    # 🔧 SMALL BATCH: Process synchronously (original logic with improved progress)
    return _process_signals_sync(signals, period_type, start_date, end_date, hours, tf, 
                                  ow_flag, analyze_beyond, strat_disabled, imp_disabled, 
                                  log_events, hide_logs_val, pre_buffer, new_ids, new_count)


def _process_signals_sync(signals, period_type, start_date, end_date, hours, tf, 
                          ow_flag, analyze_beyond, strat_disabled, imp_disabled, 
                          log_events, hide_logs_val, pre_buffer, new_ids, new_count):
    """Synchronous signal processing for small batches (<100 signals)."""
    total_signals = len(signals)
    processed_count = 0
    failed_count = 0
    failed_details = []
    
    # 🔧 DYNAMIC STEP CALCULATOR: Same as recalc - ensures ~50 progress updates
    step = max(1, total_signals // 50)
    print(f"🔥 [PARSE] Starting synchronous processing of {total_signals} signals (step={step})")
    
    buffer_ms = SIGNAL_BUFFER_MINUTES * 60 * 1000
    
    for idx, sig in enumerate(signals):
        try:
            symbol = sig['symbol']
            signal_time = sig['time_ms']
            signal_price = sig['price']
            signal_direction = sig['direction']
            # Determine start/end based on period type
            if period_type == 'date':
                if not start_date or not end_date:
                    continue
                start_dt = datetime.fromisoformat(start_date)
                end_dt = datetime.fromisoformat(end_date)
            else:  # hours
                hours = hours if hours else 1
                # Use the pre‑buffer minutes from the input (default 120)
                pre_buf_min = int(pre_buffer) if pre_buffer else 120
                pre_buffer_ms = pre_buf_min * 60 * 1000
                start_dt = datetime.fromtimestamp((signal_time - pre_buffer_ms) / 1000.0, tz=timezone.utc)
                end_dt = start_dt + timedelta(hours=hours)
            # Create task
            tid = str(uuid.uuid4())
            # Extract hide_logs preference
            hide_logs = "hide" in hide_logs_val if hide_logs_val else True
            task = DownloadTask(
                tid, [symbol], tf, 'period', start_date=start_dt, end_date=end_dt,
                overwrite=ow_flag, price_continuity_check=False,
                signal_time=signal_time, signal_price=signal_price,
                signal_symbol=symbol, signal_direction=signal_direction,
                analyze_beyond=analyze_beyond,
                enable_strategy=not strat_disabled,
                enable_impulse=not imp_disabled,
                pre_buffer_minutes=int(pre_buffer) if pre_buffer else 120,
                log_events=log_events,
                hide_logs=hide_logs
            )
            tm.add_task(task)
            # Log the signal and period details immediately
            task.add_log(f"Signal: {symbol} at {pd.to_datetime(signal_time, unit='ms', utc=True)} price={signal_price} direction={signal_direction}")
            if period_type == 'hours':
                task.add_log(f"Period: {hours} hours from signal (with {pre_buf_min} min buffer) – from {start_dt} to {end_dt}")
            else:
                task.add_log(f"Period: date range – from {start_dt.date()} to {end_dt.date()}")
            new_ids.append(tid)
            new_count += 1
            processed_count += 1
            
            # 🔧 IMPROVED: Progress logging with dynamic step (not fixed 300)
            if (idx + 1) % step == 0 or (idx + 1) == total_signals:
                progress_msg = f"✓ Progress: {idx + 1}/{total_signals} tasks created..."
                if new_ids:
                    first_task = tm.get_task(new_ids[0])
                    if first_task:
                        first_task.add_log(progress_msg)
                print(f"✅ [PARSE] {progress_msg}")
                
        except Exception as e:
            failed_count += 1
            error_msg = f"✗ Failed to create task for signal {idx}: {symbol} - {str(e)}"
            failed_details.append(f"Signal {idx} ({symbol}): {str(e)}")
            if new_ids:
                first_task = tm.get_task(new_ids[0])
                if first_task:
                    first_task.add_log(error_msg)
            print(f"⚠️ [PARSE] {error_msg}")
            continue
    
    # Final summary log
    if total_signals > 1:
        summary_msg = f"✅ Task creation complete: {processed_count} created, {failed_count} failed out of {total_signals} signals"
        if new_ids:
            first_task = tm.get_task(new_ids[0])
            if first_task:
                first_task.add_log(summary_msg)
                if failed_details:
                    first_task.add_log(f"⚠️ Failed signals: {', '.join(failed_details[:10])}" + ("..." if len(failed_details) > 10 else ""))
        print(f"🎯 [PARSE] {summary_msg}")
        if failed_details:
            print(f"⚠️ First 10 failures: {', '.join(failed_details[:10])}")
    
    return new_ids, new_count


def _process_signals_sync(signals, period_type, start_date, end_date, hours, tf, 
                          ow_flag, analyze_beyond, strat_disabled, imp_disabled, 
                          log_events, hide_logs_val, pre_buffer, new_ids, new_count):
    """Synchronous signal processing for small batches (<100 signals)."""
    total_signals = len(signals)
    processed_count = 0
    failed_count = 0
    failed_details = []
    
    # 🔧 DYNAMIC STEP CALCULATOR: Same as recalc - ensures ~50 progress updates
    step = max(1, total_signals // 50)
    print(f"🔥 [PARSE] Starting synchronous processing of {total_signals} signals (step={step})")
    
    buffer_ms = SIGNAL_BUFFER_MINUTES * 60 * 1000
    
    for idx, sig in enumerate(signals):
        try:
            symbol = sig['symbol']
            signal_time = sig['time_ms']
            signal_price = sig['price']
            signal_direction = sig['direction']
            # Determine start/end based on period type
            if period_type == 'date':
                if not start_date or not end_date:
                    continue
                start_dt = datetime.fromisoformat(start_date)
                end_dt = datetime.fromisoformat(end_date)
            else:  # hours
                hours = hours if hours else 1
                # Use the pre‑buffer minutes from the input (default 120)
                pre_buf_min = int(pre_buffer) if pre_buffer else 120
                pre_buffer_ms = pre_buf_min * 60 * 1000
                start_dt = datetime.fromtimestamp((signal_time - pre_buffer_ms) / 1000.0, tz=timezone.utc)
                end_dt = start_dt + timedelta(hours=hours)
            # Create task
            tid = str(uuid.uuid4())
            # Extract hide_logs preference
            hide_logs = "hide" in hide_logs_val if hide_logs_val else True
            task = DownloadTask(
                tid, [symbol], tf, 'period', start_date=start_dt, end_date=end_dt,
                overwrite=ow_flag, price_continuity_check=False,
                signal_time=signal_time, signal_price=signal_price,
                signal_symbol=symbol, signal_direction=signal_direction,
                analyze_beyond=analyze_beyond,
                enable_strategy=not strat_disabled,
                enable_impulse=not imp_disabled,
                pre_buffer_minutes=int(pre_buffer) if pre_buffer else 120,
                log_events=log_events,
                hide_logs=hide_logs
            )
            tm.add_task(task)
            # Log the signal and period details immediately
            task.add_log(f"Signal: {symbol} at {pd.to_datetime(signal_time, unit='ms', utc=True)} price={signal_price} direction={signal_direction}")
            if period_type == 'hours':
                task.add_log(f"Period: {hours} hours from signal (with {pre_buf_min} min buffer) – from {start_dt} to {end_dt}")
            else:
                task.add_log(f"Period: date range – from {start_dt.date()} to {end_dt.date()}")
            new_ids.append(tid)
            new_count += 1
            processed_count += 1
            
            # 🔧 IMPROVED: Progress logging with dynamic step (not fixed 300)
            if (idx + 1) % step == 0 or (idx + 1) == total_signals:
                progress_msg = f"✓ Progress: {idx + 1}/{total_signals} tasks created..."
                if new_ids:
                    first_task = tm.get_task(new_ids[0])
                    if first_task:
                        first_task.add_log(progress_msg)
                print(f"✅ [PARSE] {progress_msg}")
                
        except Exception as e:
            failed_count += 1
            error_msg = f"✗ Failed to create task for signal {idx}: {symbol} - {str(e)}"
            failed_details.append(f"Signal {idx} ({symbol}): {str(e)}")
            if new_ids:
                first_task = tm.get_task(new_ids[0])
                if first_task:
                    first_task.add_log(error_msg)
            print(f"⚠️ [PARSE] {error_msg}")
            continue
    
    # Final summary log
    if total_signals > 1:
        summary_msg = f"✅ Task creation complete: {processed_count} created, {failed_count} failed out of {total_signals} signals"
        if new_ids:
            first_task = tm.get_task(new_ids[0])
            if first_task:
                first_task.add_log(summary_msg)
                if failed_details:
                    first_task.add_log(f"⚠️ Failed signals: {', '.join(failed_details[:10])}" + ("..." if len(failed_details) > 10 else ""))
        print(f"🎯 [PARSE] {summary_msg}")
        if failed_details:
            print(f"⚠️ First 10 failures: {', '.join(failed_details[:10])}")
    
    return new_ids, new_count


def _run_parse_background(parse_data):
    """Runs in background thread to parse large signal batches without blocking UI."""
    global current_tasks
    
    print(f"🔥 [PARSE THREAD] Started with {len(parse_data['signals'])} signals")
    sys.stdout.flush()
    
    # Extract parameters
    signals = parse_data['signals']
    period_type = parse_data['period_type']
    start_date = parse_data['start_date']
    end_date = parse_data['end_date']
    hours = parse_data['hours']
    tf = parse_data['tf']
    ow_flag = parse_data['ow_flag']
    analyze_beyond = parse_data['analyze_beyond']
    strat_disabled = parse_data['strat_disabled']
    imp_disabled = parse_data['imp_disabled']
    log_events = parse_data['log_events']
    hide_logs_val = parse_data['hide_logs_val']
    pre_buffer = parse_data['pre_buffer']
    existing_ids = parse_data['existing_ids']
    existing_count = parse_data['existing_count']
    
    total_signals = len(signals)
    step = max(1, total_signals // 50)
    print(f"🔥 [PARSE THREAD] Dynamic step calculated: {step} (total={total_signals})")
    sys.stdout.flush()
    
    new_ids = existing_ids.copy()
    new_count = existing_count
    processed_count = 0
    failed_count = 0
    failed_details = []
    
    # 🔧 CRITICAL FIX: Build tasks locally first, then atomic swap at end
    # This prevents spawning hundreds of concurrent downloads immediately
    local_tasks = {}
    
    for idx, sig in enumerate(signals):
        try:
            symbol = sig['symbol']
            signal_time = sig['time_ms']
            signal_price = sig['price']
            signal_direction = sig['direction']
            
            # Determine start/end based on period type
            if period_type == 'date':
                if not start_date or not end_date:
                    continue
                start_dt = datetime.fromisoformat(start_date)
                end_dt = datetime.fromisoformat(end_date)
            else:  # hours
                h = hours if hours else 1
                pre_buf_min = int(pre_buffer) if pre_buffer else 120
                pre_buffer_ms = pre_buf_min * 60 * 1000
                start_dt = datetime.fromtimestamp((signal_time - pre_buffer_ms) / 1000.0, tz=timezone.utc)
                end_dt = start_dt + timedelta(hours=h)
            
            # Create task
            tid = str(uuid.uuid4())
            hide_logs = "hide" in hide_logs_val if hide_logs_val else True
            task = DownloadTask(
                tid, [symbol], tf, 'period', start_date=start_dt, end_date=end_dt,
                overwrite=ow_flag, price_continuity_check=False,
                signal_time=signal_time, signal_price=signal_price,
                signal_symbol=symbol, signal_direction=signal_direction,
                analyze_beyond=analyze_beyond,
                enable_strategy=not strat_disabled,
                enable_impulse=not imp_disabled,
                pre_buffer_minutes=int(pre_buffer) if pre_buffer else 120,
                log_events=log_events,
                hide_logs=hide_logs
            )
            
            # 🔧 Store locally instead of adding to TaskManager immediately
            local_tasks[tid] = task
            
            # Log details
            task.add_log(f"Signal: {symbol} at {pd.to_datetime(signal_time, unit='ms', utc=True)} price={signal_price} direction={signal_direction}")
            if period_type == 'hours':
                task.add_log(f"Period: {h} hours from signal (with {pre_buf_min} min buffer)")
            else:
                task.add_log(f"Period: date range – from {start_dt.date()} to {end_dt.date()}")
            
            new_ids.append(tid)
            new_count += 1
            processed_count += 1
            
            # 🔧 CRITICAL: Update progress counter with DYNAMIC STEP
            if (idx + 1) % step == 0 or (idx + 1) == total_signals:
                print(f"🔥 [PARSE THREAD] Progress: {idx + 1}/{total_signals} (step={step})")
                sys.stdout.flush()
            
            # 🔧 HEARTBEAT: Every 10 tasks
            if (idx + 1) % max(10, step) == 0:
                print(f"💓 [PARSE THREAD] Heartbeat: Processing signal {idx + 1}/{total_signals}...")
                sys.stdout.flush()
                
        except Exception as e:
            failed_count += 1
            failed_details.append(f"Signal {idx} ({symbol}): {str(e)}")
            print(f"⚠️ [PARSE THREAD] Task {idx} error: {e} - continuing...")
            sys.stdout.flush()
            continue
    
    # 🔧 CRITICAL: Atomic swap - add all tasks at once after parsing complete
    # This prevents race conditions and uncontrolled concurrent downloads
    with tm.lock:
        tm.tasks.update(local_tasks)
    # Queue tasks for processing (worker threads will handle them sequentially)
    for task in local_tasks.values():
        tm.queue.put(task)
    
    # Update global RAM reference
    current_tasks = list(tm.tasks.values())
    
    # Final summary
    summary_msg = f"✅ Parse complete: {processed_count} created, {failed_count} failed out of {total_signals} signals"
    if new_ids:
        first_task = tm.get_task(new_ids[0]) if new_ids else None
        if first_task:
            first_task.add_log(summary_msg)
            if failed_details:
                first_task.add_log(f"⚠️ Failed: {', '.join(failed_details[:10])}" + ("..." if len(failed_details) > 10 else ""))
    
    print(f"🎯 [PARSE THREAD] {summary_msg}")
    sys.stdout.flush()

# ----- Existing callbacks (unchanged) -----
@app.callback(
    Output("task-ids-store", "data", allow_duplicate=True),
    Input({"type": "remove-task", "index": ALL}, "n_clicks"),
    State("task-ids-store", "data"),
    prevent_initial_call=True
)
def update_task_table_only(current_page, version, lock_state, analysis_trigger):
    """Render task table ONLY. Uses aggressive caching to skip HTML generation on page changes."""
    global golden_task_store_data, golden_store_version, _page_html_cache, _cached_golden_version, cached_signal_stats_html, cached_small_stats_data, stats_cache_version
    
    # Initialize timer for full trace
    timer = PerfTimer(f"Page {current_page} Render (v{version})").start()
    
    # Validate global state
    if not hasattr(app, 'layout') or app.layout is None:
        timer.check("Validation Failed").end()
        return html.Div("", style={"display": "none"})
    
    # Get triggered input
    ctx = dash.callback_context
    if not ctx.triggered:
        timer.check("No Trigger").end()
        return dash.no_update
        
    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]
    print(f"[DEBUG] 🔍 TRIGGER: {triggered_id} | version={version} | page={current_page}")
    timer.check(f"Trigger Detected: {triggered_id}")
    
    # If only lock changed, don't re-render table
    if triggered_id == "recalc-lock-store" and version == getattr(update_task_table_only, '_last_version', None):
        print(f"[TRACE] Skipping render - lock change only")
        timer.check("Lock Skip").end()
        return dash.no_update
    
    update_task_table_only._last_version = version
    print(f"[DEBUG] 📊 STATE: golden_store_version={golden_store_version}, cache_size={len(_page_html_cache)}")
    
    # Lock check
    if lock_state and lock_state.get("locked", False):
        timer.check("Lock Active").end()
        return html.Div("⏳ Recalculating... Please wait", style={"textAlign": "center", "padding": "20px", "fontSize": "16px", "color": "#666"})
    
    # Get tasks from Golden Store
    t0 = time.time()
    if golden_task_store_data is not None and len(golden_task_store_data) > 0:
        tasks = golden_task_store_data
        print(f"[TRACE] ✓ Loaded {len(tasks)} tasks from golden store")
    else:
        with tm.lock:
            tasks = list(tm.tasks.values())
        print(f"[TRACE] ✓ Loaded {len(tasks)} tasks from task_manager")
    timer.check(f"Step 1: Get Data ({len(tasks)} tasks)")
    
    if not tasks:
        print("[TRACE] ✗ No tasks found")
        timer.end()
        return "No tasks."
    
    # CRITICAL CACHE CHECK
    current_golden_version = golden_store_version
    print(f"[TRACE] Version check: cached={_cached_golden_version}, current={current_golden_version}")
    
    # Invalidate cache if data changed
    if _cached_golden_version != current_golden_version:
        print(f"[TRACE] 🔄 Cache invalidated: {_cached_golden_version} -> {current_golden_version}")
        _page_html_cache.clear()
        _cached_golden_version = current_golden_version
        timer.check("Cache Invalidated")
    
    # ⚡ CRITICAL FIX: Cache MUST use version in key to avoid stale data
    cache_key = f"page_{current_page}_v{current_golden_version}"
    
    # Return cached page if available (INSTANT - no HTML generation)
    if cache_key in _page_html_cache:
        print(f"[TRACE] ⚡ CACHE HIT for key '{cache_key}'! Returning cached page {current_page}")
        timer.check("Cache Hit").end()
        return _page_html_cache[cache_key]
    
    print(f"[TRACE] ❌ CACHE MISS for key '{cache_key}'. Will generate rows.")
    timer.check("Cache Miss Confirmed")
    
    force_refresh = version is not None and version > 0
    
    # Pagination Slicing
    PAGE_SIZE = 300
    total_pages = max(1, (len(tasks) + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = max(0, min(current_page or 0, total_pages - 1))
    start_idx = current_page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    visible_tasks = tasks[start_idx:end_idx]
    print(f"[TRACE] ✂️ Sliced tasks [{start_idx}:{end_idx}] → {len(visible_tasks)} visible")
    timer.check(f"Step 2: Pagination Slice")
    
    # Detect if this is ONLY a page navigation (no data change)
    prev_golden_version = getattr(update_task_table_only, '_last_golden_version', None)
    is_page_only_nav = (triggered_id == "task-page-store") and (prev_golden_version is not None) and (current_golden_version == prev_golden_version)
    
    # 🔧 CRITICAL FIX: Also treat analysis_trigger as a data change (not page nav)
    # This ensures full stats are calculated after recalculation completes
    if triggered_id == "analysis-complete-trigger":
        is_page_only_nav = False
        print(f"[TRACE] 🔄 Analysis trigger detected - forcing full stats recalculation")
    
    print(f"[TRACE] Navigation detection: triggered={triggered_id}, prev_ver={prev_golden_version}, curr_ver={current_golden_version} → is_page_only_nav={is_page_only_nav}")
    timer.check("Navigation Detection")
    
    # Store current state for next comparison
    update_task_table_only._last_golden_version = current_golden_version
    update_task_table_only._last_page = current_page
    
    # Pre-calculate helper functions ONCE - OPTIMIZED with native datetime
    from datetime import datetime, timezone
    
    def fmt_time(ts):
        """⚡ ULTRA-FAST timestamp formatting - NO pandas calls"""
        if ts is None: return "-"
        try:
            if isinstance(ts, (float, np.floating)) and pd.isna(ts): return "-"
            if isinstance(ts, (datetime, pd.Timestamp)):
                return ts.strftime("%Y-%m-%d %H:%M")
            if isinstance(ts, str):
                # ⚡ FAST PATH: Handle ISO-8601 strings directly (85x faster than pandas)
                ts_clean = ts.strip()
                if ts_clean.endswith('Z'):
                    ts_clean = ts_clean[:-1]
                if 'T' in ts_clean:
                    # ISO format: 2024-01-15T10:30:45.123
                    if '.' in ts_clean:
                        dt = datetime.strptime(ts_clean.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                    else:
                        dt = datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S")
                    return dt.strftime("%Y-%m-%d %H:%M")
                # Try numeric string
                try:
                    ts_num = float(ts_clean)
                    return datetime.fromtimestamp(ts_num / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            # Numeric timestamp (milliseconds)
            if isinstance(ts, (int, float)):
                return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "-"
        # Fallback to pandas (slow path - should rarely happen)
        try:
            return pd.to_datetime(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "-"
    
    def fmt_dd(val):
        if val is None: return "-"
        if isinstance(val, (float, np.floating)) and pd.isna(val): return "-"
        try:
            return f"{float(val):.2f}%"
        except Exception:
            return "-"
    
    timer.check("Step 3: Helper Functions Setup")
    
    # Generate rows for visible tasks ONLY (300 max)
    print(f"[TRACE] 🚀 Starting row generation for {len(visible_tasks)} tasks...")
    rows = []
    row_count = 0
    t_row_start = time.time()
    
    for t in visible_tasks:
        row_count += 1
        # ⚡ OPTIMIZATION: Direct attribute access instead of getattr where possible
        direction_display = t.signal_direction if t.signal_direction else "-"
        # ⚡ CRITICAL OPTIMIZATION: Use fmt_time helper (native datetime) instead of pd.to_datetime
        signal_time_display = fmt_time(t.signal_time) if t.signal_time else "-"
        first_event_display = fmt_time(t.first_event_time)
        pin_display = "Yes" if t.first_event_is_pin else "No" if t.first_event_time else "-"
        price_change_display = f"{t.price_change_pct:.2f}%" if t.price_change_pct is not None else "-"
        reached_display = "Yes" if t.reached_level else "No"
        reversed_display = "Yes" if t.reversed_direction else "No"
        hit_1_display = "Yes" if t.hit_1 else "No"
        hit_1_5_display = "Yes" if t.hit_1_5 else "No"
        hit_2_display = "Yes" if t.hit_2 else "No"
        
        strategy_display = t.strategy_log_summary if t.strategy_log_summary else '-'
        strategy_conf = t.strategy_confidence if t.strategy_confidence else 0
        confidence_display = f"{strategy_conf:.1f}%" if strategy_conf else "-"
        
        # ⚡ OPTIMIZATION: Count impulses once and cache
        impulse_count = sum(1 for sig in t.strategy_signals if sig.get('type') == 'impulse')
        impulse_display = str(impulse_count)
        
        # ✅ LOGIC: Respect the hide_logs checkbox setting per task
        if t.hide_logs:
            log_display = html.Span("Logs are hidden", style={"color": "#888", "fontStyle": "italic", "fontSize": "12px"})
        else:
            # 🔧 PERFORMANCE: Replace heavy dcc.Textarea with lightweight html.Div
            log_text = "\n".join(t.log) if t.log else "No logs yet..."
            log_display = html.Div(
                log_text,
                style={
                    "width": "100%", 
                    "maxHeight": "100px", 
                    "minHeight": "50px",
                    "fontFamily": "monospace", 
                    "fontSize": "11px", 
                    "overflowY": "auto",
                    "whiteSpace": "pre-wrap",
                    "wordWrap": "break-word",
                    "padding": "4px",
                    "border": "1px solid #ddd",
                    "borderRadius": "3px",
                    "backgroundColor": "#fafafa"
                }
            )
            
        # 🔧 PERFORMANCE: Build buttons with minimal operations
        task_id_str = str(t.task_id)
        is_completed = t.status == "completed"
        btn_disabled = "not-allowed" if not is_completed else "pointer"
        btn_opacity = "0.6" if not is_completed else "1"
        
        stop_btn = html.Div("Stop", id=f"btn-stop-{task_id_str}",
            **{"data-action": "stop", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "4px 8px", "backgroundColor": "#ffcccc", 
                   "borderRadius": "3px", "cursor": "pointer", "display": "inline-block", "fontSize": "11px"},
            className="interactive-button")
        
        pause_label = "Resume" if t.paused else "Pause"
        pause_bg = "#fff3cd" if t.paused else "#d1ecf1"
        pause_btn = html.Div(pause_label, id=f"btn-pause-{task_id_str}",
            **{"data-action": "pause", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "4px 8px", "backgroundColor": pause_bg, 
                   "borderRadius": "3px", "cursor": "pointer", "display": "inline-block", "fontSize": "11px"},
            className="interactive-button")
        
        chart_btn = html.Div("Chart", id=f"btn-chart-{task_id_str}",
            **{"data-action": "chart", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "4px 8px", "backgroundColor": "#d4edda" if is_completed else "#e9ecef", 
                   "borderRadius": "3px", "cursor": btn_disabled, "display": "inline-block", 
                   "fontSize": "11px", "opacity": btn_opacity},
            className="interactive-button")
        
        details_btn = html.Div("Details", id=f"btn-details-{task_id_str}",
            **{"data-action": "details", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "4px 8px", "backgroundColor": "#d4edda" if is_completed else "#e9ecef", 
                   "borderRadius": "3px", "cursor": btn_disabled, "display": "inline-block", 
                   "fontSize": "11px", "opacity": btn_opacity},
            className="interactive-button")
        
        impulse_has_data = is_completed and impulse_count > 0
        impulse_btn = html.Div("Impulse", id=f"btn-impulse-{task_id_str}",
            **{"data-action": "impulse", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "4px 8px", "backgroundColor": "#d4edda" if impulse_has_data else "#e9ecef", 
                   "borderRadius": "3px", "cursor": "pointer" if impulse_has_data else "not-allowed", 
                   "display": "inline-block", "fontSize": "11px", 
                   "opacity": "1" if impulse_has_data else "0.6"},
            className="interactive-button")
        
        rerun_strat_btn = html.Div("Re‑run Strategy", id=f"btn-rerun-strat-{task_id_str}",
            **{"data-action": "rerun-strat", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "3px 6px", "backgroundColor": "#d4edda" if is_completed else "#e9ecef", 
                   "borderRadius": "3px", "cursor": btn_disabled, "display": "inline-block", 
                   "fontSize": "9px", "opacity": btn_opacity},
            className="interactive-button")
        
        rerun_impulse_btn = html.Div("Re‑run Impulse", id=f"btn-rerun-impulse-{task_id_str}",
            **{"data-action": "rerun-impulse", "data-task-id": task_id_str},
            style={"margin": "2px", "padding": "3px 6px", "backgroundColor": "#d4edda" if is_completed else "#e9ecef", 
                   "borderRadius": "3px", "cursor": btn_disabled, "display": "inline-block", 
                   "fontSize": "9px", "opacity": btn_opacity},
            className="interactive-button")
        
        # 📺 TV Button
        symbol = t.symbols[0] if t.symbols else ""
        tv_url = f"https://www.tradingview.com/chart/?symbol=BYBIT:{symbol}&interval={t.timeframe}"
        tv_btn = html.A(
            html.Div("TV", style={"margin": "2px", "padding": "4px 8px", "backgroundColor": "#e7f3ff", 
                                  "borderRadius": "3px", "cursor": "pointer", "display": "inline-block", "fontSize": "11px"}),
            href=tv_url, target="_blank", title="Open TradingView Chart"
        )

        button_cell = html.Div([stop_btn, pause_btn, chart_btn, details_btn, impulse_btn, rerun_strat_btn, rerun_impulse_btn, tv_btn])

        # ⚡ OPTIMIZATION: Use cached attribute values
        rows.append(html.Tr([
            html.Td(task_id_str[:8], style={"minWidth": "80px"}),
            html.Td(t.status, style={"minWidth": "80px"}),
            html.Td(f"{t.progress:.1f}%", style={"minWidth": "70px"}),
            html.Td(", ".join(t.symbols), style={"minWidth": "100px"}),
            html.Td(t.mode, style={"minWidth": "70px"}),
            html.Td(direction_display, style={"minWidth": "80px"}),
            html.Td(signal_time_display, style={"minWidth": "120px"}),
            html.Td(first_event_display, style={"minWidth": "120px"}),
            html.Td(pin_display, style={"minWidth": "60px"}),
            html.Td(price_change_display, style={"minWidth": "80px"}),
            html.Td(reached_display, style={"minWidth": "70px"}),
            html.Td(reversed_display, style={"minWidth": "70px"}),
            html.Td(hit_1_display, style={"minWidth": "50px"}),
            html.Td(hit_1_5_display, style={"minWidth": "60px"}),
            html.Td(hit_2_display, style={"minWidth": "50px"}),
            html.Td("Yes" if t.first_hit_1_expected else "No", style={"minWidth": "50px"}),
            html.Td(fmt_time(t.first_hit_1_expected_time), style={"minWidth": "140px"}),
            html.Td("Yes" if t.first_hit_1_5_expected else "No", style={"minWidth": "60px"}),
            html.Td(fmt_time(t.first_hit_1_5_expected_time), style={"minWidth": "140px"}),
            html.Td("Yes" if t.first_hit_2_expected else "No", style={"minWidth": "50px"}),
            html.Td(fmt_time(t.first_hit_2_expected_time), style={"minWidth": "140px"}),
            html.Td("Yes" if t.first_hit_1_opposite else "No", style={"minWidth": "50px"}),
            html.Td(fmt_time(t.first_hit_1_opposite_time), style={"minWidth": "140px"}),
            html.Td("Yes" if t.first_hit_1_5_opposite else "No", style={"minWidth": "60px"}),
            html.Td(fmt_time(t.first_hit_1_5_opposite_time), style={"minWidth": "140px"}),
            html.Td("Yes" if t.first_hit_2_opposite else "No", style={"minWidth": "50px"}),
            html.Td(fmt_time(t.first_hit_2_opposite_time), style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.max_adverse_move_pct), style={"minWidth": "100px"}, className="strike-through" if not t.reached_level else ""),
            html.Td(fmt_time(t.max_adverse_time), style={"minWidth": "140px"}, className="strike-through" if not t.reached_level else ""),
            html.Td(fmt_dd(t.max_expected_move_pct), style={"minWidth": "100px"}, className="strike-through" if not t.reached_level else ""),
            html.Td(fmt_time(t.max_expected_time), style={"minWidth": "140px"}, className="strike-through" if not t.reached_level else ""),
            html.Td("Not returned" if not t.returned_to_signal else fmt_dd(t.max_adverse_before_return_pct), style={"minWidth": "140px"}),
            html.Td(fmt_time(t.max_adverse_before_return_time) if t.returned_to_signal else "-", style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.max_adverse_sgnl_pct), style={"minWidth": "100px"}),
            html.Td(fmt_time(t.max_adverse_sgnl_time), style={"minWidth": "140px"}),
            html.Td("Not returned" if not t.returned_to_sgnl else fmt_dd(t.max_adverse_before_return_sgnl_pct), style={"minWidth": "140px"}),
            html.Td(fmt_time(t.max_adverse_before_return_sgnl_time) if t.returned_to_sgnl else "-", style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.max_expected_sgnl_pct), style={"minWidth": "100px"}),
            html.Td(fmt_time(t.max_expected_sgnl_time), style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.drawdown_before_level), style={"minWidth": "80px"}),
            html.Td(fmt_time(t.drawdown_before_level_time), style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.drawdown_before_1pct), style={"minWidth": "80px"}),
            html.Td(fmt_time(t.drawdown_before_1pct_time), style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.drawdown_before_1_5pct), style={"minWidth": "80px"}),
            html.Td(fmt_time(t.drawdown_before_1_5pct_time), style={"minWidth": "140px"}),
            html.Td(fmt_dd(t.drawdown_before_2pct), style={"minWidth": "80px"}),
            html.Td(fmt_time(t.drawdown_before_2pct_time), style={"minWidth": "140px"}),
            html.Td(strategy_display, style={"minWidth": "120px"}),
            html.Td(confidence_display, style={"minWidth": "80px"}),
            html.Td(impulse_display, style={"minWidth": "80px"}),
            html.Td(log_display, style={"minWidth": "200px"}),
            html.Td(button_cell, style={"minWidth": "180px"})
        ]))
        
        # Log progress every 100 rows
        if row_count % 100 == 0:
            elapsed = time.time() - t_row_start
            print(f"[TRACE]   └─ Generated {row_count}/{len(visible_tasks)} rows ({elapsed:.2f}s)")
    
    row_elapsed = time.time() - t_row_start
    print(f"[TRACE] ✓ Generated {row_count} rows in {row_elapsed:.2f}s ({row_elapsed/row_count*1000:.1f}ms per row)")
    timer.check(f"Step 4: Row Generation ({row_count} rows)")
    
    # Build table HTML
    t_table_start = time.time()
    table = html.Table([
        html.Thead(html.Tr([
            html.Th("ID", style={"minWidth": "80px"}),
            html.Th("Status", style={"minWidth": "80px"}),
            html.Th("Progress", style={"minWidth": "70px"}),
            html.Th("Symbols", style={"minWidth": "100px"}),
            html.Th("Mode", style={"minWidth": "70px"}),
            html.Th("Direction", style={"minWidth": "80px"}),
            html.Th("Signal Time", style={"minWidth": "120px"}),
            html.Th("First Event", style={"minWidth": "120px"}),
            html.Th("Pin?", style={"minWidth": "60px"}),
            html.Th("Price Δ% (sgnl-lvl)", style={"minWidth": "80px"}),
            html.Th("Reached", style={"minWidth": "70px"}),
            html.Th("Reversed", style={"minWidth": "70px"}),
            html.Th("Hit 1% (lvl-fwd.dir)", style={"minWidth": "50px"}),
            html.Th("Hit 1.5% (lvl-fwd.dir)", style={"minWidth": "60px"}),
            html.Th("Hit 2% (lvl-fwd.dir)", style={"minWidth": "50px"}),
            html.Th("1st 1% Exp", style={"minWidth": "50px"}),
            html.Th("Time 1% Exp", style={"minWidth": "140px"}),
            html.Th("1st 1.5% Exp", style={"minWidth": "60px"}),
            html.Th("Time 1.5% Exp", style={"minWidth": "140px"}),
            html.Th("1st 2% Exp", style={"minWidth": "50px"}),
            html.Th("Time 2% Exp", style={"minWidth": "140px"}),
            html.Th("1st 1% Opp", style={"minWidth": "50px"}),
            html.Th("Time 1% Opp", style={"minWidth": "140px"}),
            html.Th("1st 1.5% Opp", style={"minWidth": "60px"}),
            html.Th("Time 1.5% Opp", style={"minWidth": "140px"}),
            html.Th("1st 2% Opp", style={"minWidth": "50px"}),
            html.Th("Time 2% Opp", style={"minWidth": "140px"}),
            html.Th("Max Adv %(lvl)", style={"minWidth": "100px"}),
            html.Th("Max Adv T(lvl)", style={"minWidth": "140px"}),
            html.Th("Max Exp %(lvl)", style={"minWidth": "100px"}),
            html.Th("Max Exp T(lvl)", style={"minWidth": "140px"}),
            html.Th("Max Adv %(sgnl)", style={"minWidth": "100px"}),
            html.Th("Max Adv T(sgnl)", style={"minWidth": "140px"}),
            html.Th("Max Exp %(sgnl)", style={"minWidth": "100px"}),
            html.Th("Max Exp T(sgnl)", style={"minWidth": "140px"}),           
            html.Th("Max Adv %(bef ret lvl)", style={"minWidth": "140px"}),
            html.Th("Time (bef ret lvl)", style={"minWidth": "140px"}),
            html.Th("Max Adv %(bef ret sgnl)", style={"minWidth": "140px"}),
            html.Th("Time (bef ret sgnl)", style={"minWidth": "140px"}),
            html.Th("DD% (Lvl)", style={"minWidth": "80px"}),
            html.Th("DD Time (Lvl)", style={"minWidth": "140px"}),
            html.Th("DD% (1%)", style={"minWidth": "80px"}),
            html.Th("DD Time (1%)", style={"minWidth": "140px"}),
            html.Th("DD% (1.5%)", style={"minWidth": "80px"}),
            html.Th("DD Time (1.5%)", style={"minWidth": "140px"}),
            html.Th("DD% (2%)", style={"minWidth": "80px"}),
            html.Th("DD Time (2%)", style={"minWidth": "140px"}),
            html.Th("Strategy", style={"minWidth": "120px"}),
            html.Th("Confidence", style={"minWidth": "80px"}),
            html.Th("Impulse #", style={"minWidth": "80px"}),
            html.Th("Log", style={"minWidth": "200px"}),
            html.Th("Actions", style={"minWidth": "180px"})
        ]), style={'position': 'sticky', 'top': 0, 'backgroundColor': '#f0f0f0', 'zIndex': 10}),
        html.Tbody(rows)
    ], style={"width": "100%", "borderCollapse": "collapse"})
    print(f"[TRACE] ✓ Built table HTML in {time.time() - t_table_start:.2f}s")
    timer.check("Step 5: Build Table HTML")

    # ⚡ PERFORMANCE: Skip heavy stats calculation on page-only navigation
    # This is the CRITICAL FIX - stats are calculated ONLY when version changes (data reload/recalc)
    if is_page_only_nav:
        # Return minimal stats for page navigation (no heavy iteration over all tasks)
        # But we still need to show basic stats from ALL tasks (consistent across pages)
        total_tasks = len(tasks)
        completed_count = sum(1 for t in tasks if t.status == "completed")
        
        # ALL-task averages (still fast - just iterating, not generating HTML)
        avg_adv = np.mean([t.max_adverse_move_pct for t in tasks if t.max_adverse_move_pct is not None and not pd.isna(t.max_adverse_move_pct)] or [0])
        avg_dd = np.mean([t.drawdown_before_level for t in tasks if t.drawdown_before_level is not None and not pd.isna(t.drawdown_before_level)] or [0])
        
        stats_rows = [
            html.Tr([html.Td("✅ Task Completed (Total)"), html.Td(str(completed_count))]),
            html.Tr([html.Td("📦 Total Tasks"), html.Td(str(total_tasks))]),
            html.Tr([html.Td("📉 Avg Max Adverse (All)"), html.Td(fmt_dd(avg_adv))]),
            html.Tr([html.Td("📉 Avg Drawdown Lvl (All)"), html.Td(fmt_dd(avg_dd))])
        ]
        stats_table = html.Table([html.Tbody(stats_rows)], style={"border": "1px solid #ccc", "padding": "5px", "fontSize": "13px", "backgroundColor": "#f9f9f9"})
        
        # 🔧 FIX: Use cached signal stats from ALL tasks (calculated once per version)
        print(f"[DEBUG] ⏭️ USING CACHED SIGNAL STATS")
        stats_elapsed = 0.0
        # Access global cache (already declared at function level)
        signal_stats_table = cached_signal_stats_html if cached_signal_stats_html else html.Div("ℹ️ Stats loading...", style={"textAlign": "center", "padding": "10px", "color": "#555", "fontStyle": "italic"})
    else:
        # 🔧 CRITICAL: Calculate signal stats on ALL tasks when data loads/recalculates
        print(f"[DEBUG] 🚀 CALCULATING SIGNAL STATS for {len(tasks)} tasks...")
        
        t_stats_start = time.time()

        # ✅ BASIC STATS: Calculate only when data changes (not on page nav) - NOW USES ALL TASKS
        total_tasks = len(tasks)
        completed_count = sum(1 for t in tasks if t.status == "completed")

        # ALL-task averages (consistent across all pages)
        avg_adv = np.mean([t.max_adverse_move_pct for t in tasks if t.max_adverse_move_pct is not None and not pd.isna(t.max_adverse_move_pct)] or [0])
        avg_dd = np.mean([t.drawdown_before_level for t in tasks if t.drawdown_before_level is not None and not pd.isna(t.drawdown_before_level)] or [0])

        stats_rows = [
            html.Tr([html.Td("✅ Task Completed (Total)"), html.Td(str(completed_count))]),
            html.Tr([html.Td("📦 Total Tasks"), html.Td(str(total_tasks))]),
            html.Tr([html.Td("📉 Avg Max Adverse (All)"), html.Td(fmt_dd(avg_adv))]),
            html.Tr([html.Td("📉 Avg Drawdown Lvl (All)"), html.Td(fmt_dd(avg_dd))])
        ]
        stats_table = html.Table([html.Tbody(stats_rows)], style={"border": "1px solid #ccc", "padding": "5px", "fontSize": "13px", "backgroundColor": "#f9f9f9"})

        # ✅ SIGNAL STATS: Calculated on ALL in-memory tasks (consistent denominator)
        reached_level_cnt = sum(1 for t in tasks if t.reached_level)
        reversed_dir_cnt = sum(1 for t in tasks if t.reversed_direction)
        hit_1_cnt = sum(1 for t in tasks if t.reached_level and t.hit_1)
        hit_1_5_cnt = sum(1 for t in tasks if t.reached_level and t.hit_1_5)
        hit_2_cnt = sum(1 for t in tasks if t.reached_level and t.hit_2)
        
        def fmt_stat(stat_count, total):
            if total == 0: return "0 / 0 (0.0%)"
            return f"{stat_count} / {total} ({(stat_count/total)*100:.1f}%)"

        # ----- Max Adverse Distribution Stats (compact format) -----
        def get_adverse_range(pct):
            if pct is None or (isinstance(pct, float) and pd.isna(pct)):
                return None
            if 0 <= pct < 0.5: return "0-0.5%"
            elif 0.5 <= pct < 1: return "0.5-1%"
            elif 1 <= pct < 2: return "1-2%"
            elif 2 <= pct < 3: return "2-3%"
            elif 3 <= pct < 4: return "3-4%"
            elif 4 <= pct < 5: return "4-5%"
            elif 5 <= pct < 10: return "5-10%"
            elif 10 <= pct < 20: return "10-20%"
            elif 20 <= pct < 30: return "20-30%"
            elif pct >= 30: return ">30%"
            return None

        # Count tasks in each adverse range (only for reached_level tasks)
        adverse_counts = {}
        for t in tasks:
            adv = t.max_adverse_move_pct
            if t.reached_level and adv is not None and not (isinstance(adv, float) and pd.isna(adv)):
                range_key = get_adverse_range(adv)
                if range_key:
                    adverse_counts[range_key] = adverse_counts.get(range_key, 0) + 1

        # Format as two compact rows (5 ranges each) to save vertical space
        ranges = ["0-0.5%", "0.5-1%", "1-2%", "2-3%", "3-4%", "4-5%", "5-10%", "10-20%", "20-30%", ">30%"]
        row1_adv = " | ".join([f"{r}:{adverse_counts.get(r,0)}" for r in ranges[:5]])
        row2_adv = " | ".join([f"{r}:{adverse_counts.get(r,0)}" for r in ranges[5:]])

        # 🔧 Calculate cumulative totals for Max Adverse
        adv_05_plus_total = 0
        adv_4_plus_total = 0
        for t in tasks:
            adv = t.max_adverse_move_pct
            if t.reached_level and adv is not None and not (isinstance(adv, float) and pd.isna(adv)):
                if adv >= 0.5:
                    adv_05_plus_total += 1
                if adv >= 4.0:
                    adv_4_plus_total += 1

        # 🔧 NEW: Calculate distribution & cumulative totals for Max Expected
        exp_counts = {}
        exp_05_plus_total = 0
        exp_4_plus_total = 0
        for t in tasks:
            exp = t.max_expected_move_pct
            if t.reached_level and exp is not None and not (isinstance(exp, float) and pd.isna(exp)):
                range_key = get_adverse_range(exp)
                if range_key:
                    exp_counts[range_key] = exp_counts.get(range_key, 0) + 1
                if exp >= 0.5:
                    exp_05_plus_total += 1
                if exp >= 4.0:
                    exp_4_plus_total += 1
                
        row1_exp = " | ".join([f"{r}:{exp_counts.get(r,0)}" for r in ranges[:5]])
        row2_exp = " | ".join([f"{r}:{exp_counts.get(r,0)}" for r in ranges[5:]])

        # Define uniform style for all cells in the summary table
        td_style = {"fontSize": "13px", "fontWeight": "normal", "padding": "2px 5px"}
        
        # Calculate (sgnl) statistics for Adverse & Expected - OPTIMIZED with direct attribute access
        adv_sgnl_counts = {}; exp_sgnl_counts = {}
        adv_sgnl_05 = 0; adv_sgnl_4 = 0; exp_sgnl_05 = 0; exp_sgnl_4 = 0
        for t in tasks:
            adv_s = t.max_adverse_sgnl_pct
            if adv_s is not None and not (isinstance(adv_s, float) and pd.isna(adv_s)):
                r = get_adverse_range(adv_s)
                if r: adv_sgnl_counts[r] = adv_sgnl_counts.get(r, 0) + 1
                if adv_s >= 0.5: adv_sgnl_05 += 1
                if adv_s >= 4.0: adv_sgnl_4 += 1
            exp_s = t.max_expected_sgnl_pct
            if exp_s is not None and not (isinstance(exp_s, float) and pd.isna(exp_s)):
                r = get_adverse_range(exp_s)
                if r: exp_sgnl_counts[r] = exp_sgnl_counts.get(r, 0) + 1
                if exp_s >= 0.5: exp_sgnl_05 += 1
                if exp_s >= 4.0: exp_sgnl_4 += 1
                
        row1_adv_s = " | ".join([f"{r}:{adv_sgnl_counts.get(r,0)}" for r in ranges[:5]])
        row2_adv_s = " | ".join([f"{r}:{adv_sgnl_counts.get(r,0)}" for r in ranges[5:]])
        row1_exp_s = " | ".join([f"{r}:{exp_sgnl_counts.get(r,0)}" for r in ranges[:5]])
        row2_exp_s = " | ".join([f"{r}:{exp_sgnl_counts.get(r,0)}" for r in ranges[5:]])
        
        # Delta Price (sgnl to lvl) Distribution
        delta_counts = {k: 0 for k in ranges}
        delta_05_plus_total = 0
        delta_4_plus_total = 0
        for t in tasks:
            dp = t.price_change_pct
            if dp is not None and not (isinstance(dp, float) and pd.isna(dp)):
                val = abs(dp)
                r = get_adverse_range(val)
                if r:
                    delta_counts[r] += 1
                if val >= 0.5: delta_05_plus_total += 1
                if val >= 4.0: delta_4_plus_total += 1

        row1_delta = " | ".join([f"{r}:{delta_counts[r]}" for r in ranges[:5]])
        row2_delta = " | ".join([f"{r}:{delta_counts[r]}" for r in ranges[5:]])

        signal_stats_rows = [
            html.Tr([html.Td("Reached Level", style=td_style), html.Td(fmt_stat(reached_level_cnt, total_tasks), style=td_style)]),
            html.Tr([html.Td("Reversed Direction", style=td_style), html.Td(fmt_stat(reversed_dir_cnt, total_tasks), style=td_style)]),
            html.Tr([html.Td("Hit 1% (from level)", style=td_style), html.Td(fmt_stat(hit_1_cnt, total_tasks), style=td_style)]),
            html.Tr([html.Td("Hit 1.5% (from level)", style=td_style), html.Td(fmt_stat(hit_1_5_cnt, total_tasks), style=td_style)]),
            html.Tr([html.Td("Hit 2% (from level)", style=td_style), html.Td(fmt_stat(hit_2_cnt, total_tasks), style=td_style)]),
            # Max Adverse (lvl) Rows
            html.Tr([html.Td("Max Adv 0-4% (lvl)", style=td_style), html.Td(row1_adv, style=td_style)]),
            html.Tr([html.Td("Max Adv 4%+ (lvl)", style=td_style), html.Td(row2_adv, style=td_style)]),
            html.Tr([html.Td("Max Adv 0.5%+ Total (lvl)", style=td_style), html.Td(str(adv_05_plus_total), style=td_style)]),
            html.Tr([html.Td("Max Adv 4%+ Total (lvl)", style=td_style), html.Td(str(adv_4_plus_total), style=td_style)]),
            # Max Expected (lvl) Rows
            html.Tr([html.Td("Max Exp 0-4% (lvl)", style=td_style), html.Td(row1_exp, style=td_style)]),
            html.Tr([html.Td("Max Exp 4%+ (lvl)", style=td_style), html.Td(row2_exp, style=td_style)]),
            html.Tr([html.Td("Max Exp 0.5%+ Total (lvl)", style=td_style), html.Td(str(exp_05_plus_total), style=td_style)]),
            html.Tr([html.Td("Max Exp 4%+ Total (lvl)", style=td_style), html.Td(str(exp_4_plus_total), style=td_style)]),
            # Max Adverse (sgnl) Rows
            html.Tr([html.Td("Max Adv 0-4% (sgnl)", style=td_style), html.Td(row1_adv_s, style=td_style)]),
            html.Tr([html.Td("Max Adv 4%+ (sgnl)", style=td_style), html.Td(row2_adv_s, style=td_style)]),
            html.Tr([html.Td("Max Adv 0.5%+ Total (sgnl)", style=td_style), html.Td(str(adv_sgnl_05), style=td_style)]),
            html.Tr([html.Td("Max Adv 4%+ Total (sgnl)", style=td_style), html.Td(str(adv_sgnl_4), style=td_style)]),
            # Max Expected (sgnl) Rows
            html.Tr([html.Td("Max Exp 0-4% (sgnl)", style=td_style), html.Td(row1_exp_s, style=td_style)]),
            html.Tr([html.Td("Max Exp 4%+ (sgnl)", style=td_style), html.Td(row2_exp_s, style=td_style)]),
            html.Tr([html.Td("Max Exp 0.5%+ Total (sgnl)", style=td_style), html.Td(str(exp_sgnl_05), style=td_style)]),
            html.Tr([html.Td("Max Exp 4%+ Total (sgnl)", style=td_style), html.Td(str(exp_sgnl_4), style=td_style)]),
            # Delta Price Rows
            html.Tr([html.Td("Delta Price 0-4%", style=td_style), html.Td(row1_delta, style=td_style)]),
            html.Tr([html.Td("Delta Price 4%+", style=td_style), html.Td(row2_delta, style=td_style)]),
            html.Tr([html.Td("Delta Price 0.5%+ Total", style=td_style), html.Td(str(delta_05_plus_total), style=td_style)]),
            html.Tr([html.Td("Delta Price 4%+ Total", style=td_style), html.Td(str(delta_4_plus_total), style=td_style)]),
        ]
        signal_stats_table = html.Table([html.Tbody(signal_stats_rows)], style={"border": "1px solid #4a90e2", "padding": "5px", "marginTop": "10px", "backgroundColor": "#f0f7ff"})
        
        # Cache the stats for ALL tasks (calculated once per version)
        cached_signal_stats_html = signal_stats_table
        cached_small_stats_data = {"completed": completed_count, "total": total_tasks, "avg_adv": avg_adv, "avg_dd": avg_dd}
        stats_cache_version = golden_store_version
        
        stats_elapsed = time.time() - t_stats_start
        print(f"[DEBUG] ✅ SIGNAL STATS COMPLETE in {stats_elapsed:.2f}s (cached for version {stats_cache_version})")
    
    # 🔧 PAGINATION NAVIGATION
    nav_buttons = []
    nav_buttons.append(html.Button("<< Prev", id={"type":"page-nav","index":"prev"}, disabled=(current_page==0), style={"margin":"2px"}))
    for p in range(total_pages):
        btn_style = {"margin":"2px", "padding":"2px 6px", "fontWeight":"bold" if p==current_page else "normal"}
        nav_buttons.append(html.Button(str(p+1), id={"type":"page-nav","index":p}, style=btn_style))
    nav_buttons.append(html.Button("Next >>", id={"type":"page-nav","index":"next"}, disabled=(current_page==total_pages-1), style={"margin":"2px"}))
    nav_container = html.Div(nav_buttons, style={"display":"flex", "alignItems":"center", "marginBottom":"8px", "justifyContent":"center"})
    timer.check("Step 7: Build Pagination Nav")

    result = html.Div([
        html.H4("Task Summary"),
        nav_container,
        html.Div(table, style={"overflow-x": "auto", "overflow-y": "auto", "max-height": "75vh", "width": "100%"}),
        html.P(f"📄 Page {current_page+1} of {total_pages} | Showing tasks {start_idx+1}-{min(end_idx, len(tasks))} of {len(tasks)}", style={"textAlign":"center", "fontSize":"12px", "color":"#555"}),
        stats_table,
        html.H5("Signal Performance Summary", style={"marginTop": "15px", "marginBottom": "5px"}),
        signal_stats_table,
        html.P(
            "ℹ️ Hit % metrics measure price movement ≥1%/1.5%/2% **in the EXPECTED direction** from the signal level base. "
            "Resistance: Price moves UP ≥X% from level. Support: Price moves DOWN ≥X% from level. "
            "Hits are only counted if the price actually touched the level first.",
            style={"fontSize": "11px", "color": "#777", "marginTop": "6px", "marginBottom": "0", "fontStyle": "italic"}
        )
    ])
    timer.check("Step 8: Build Final Result Div")
    
    # ⚡ CACHE THE RESULT with version key for instant page switching (ALWAYS cache, regardless of stats)
    # The table HTML is the same whether we calculated full stats or page-only stats
    _page_html_cache[cache_key] = result
    timer.check("Step 9: Cache Result")
    
    # Print final timing
    timer.end()
    print(f"[TRACE] <<< COMPLETE Page {current_page} rendered in {timer.last_time - timer.start_time:.4f}s | Cache Size: {len(_page_html_cache)}")
    print(f"[TRACE] ✓✓✓ RETURNING RESULT TO DASH UI ✓✓✓")
    
    return result

@app.callback(
    Output("task-page-store", "data"),
    Input({"type": "page-nav", "index": ALL}, "n_clicks"),
    State("task-count-store", "data"),
    State("task-page-store", "data"),
    prevent_initial_call=True
)
def rerun_impulse(n_clicks_list):
    # FIX: Stop phantom triggers caused by table re-rendering
    if not any(n_clicks_list):
        return no_update

    triggered = ctx.triggered_id
    if not triggered or not isinstance(triggered, dict):
        return no_update
    task_id = triggered.get("index")
    task = tm.get_task(task_id)
    if not task or task.status != "completed":
        return no_update
    try:
        task.run_impulse_detection(verbose=False)
        task.add_log("Manual impulse re‑run completed")
        return no_update
    except Exception as e:
        task.add_log(f"Manual impulse re‑run error: {e}")
        return no_update

@app.callback(
    Output("impulse-apply-all-status", "children", allow_duplicate=True),
    Input("rerun-strat-all", "n_clicks"),
    State("impulse-range-mult", "value"),
    State("impulse-vol-mult", "value"),
    State("impulse-body-ratio", "value"),
    State("impulse-wick-ratio", "value"),
    State("impulse-next-confirm", "value"),
    State("impulse-rsi-divergence", "value"),
    State("impulse-rsi-extreme", "value"),
    State("impulse-base-candle", "value"),
    State("impulse-vol-accel", "value"),
    prevent_initial_call=True
)
def save_tasks_to_json(n, filename):
    """
    Save tasks using the 'Reconstruction from Truth' pattern.
    
    This function implements the Serialization Bridge architecture:
    1. Source of Truth: Reads from live RAM objects (task_manager.tasks)
    2. Sanitization: Converts all types via sanitize_for_json()
    3. Graveyard Preservation: Invalid tasks are preserved from original JSON
    4. Atomic Save: Uses temp file + replace for crash safety
    
    Data Layers:
    - core_signal: Static configuration (symbol, timeframe, signal_text, etc.)
    - analysis_results: Dynamic calculations (drawdown, events, strategies)
    - system_meta: Technical metadata (version, timestamp, status)
    """
    if not filename:
        return "⚠️ Please enter a valid filename.", filename
    
    # Sanitize filename & ensure .json extension
    filename = re.sub(r'[^\w\-_.]', '_', filename.strip())
    if not filename.endswith('.json'):
        filename += '.json'
    
    # Ensure the task_logs directory exists
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    filepath = os.path.join(LOGS_DIR, filename)
    
    # Get live tasks from RAM (Source of Truth)
    tasks = tm.get_all_tasks()
    
    # Build reconstructed data list
    serializable_data = []
    
    # Process all valid tasks from RAM
    if tasks:
        for t in tasks:
            d = {}
            # Iterate through all attributes, excluding non-serializable threading objects
            for k, v in t.__dict__.items():
                # Skip threading/synchronization objects and internal caches
                if k in ('stop_event', 'pause_event', 'state_lock', 'raw_batches', '_chart_cache', 'symbol_ranges'):
                    continue
                
                # Apply sanitize_for_json to ALL values (handles datetime, NumPy, NaN, etc.)
                d[k] = sanitize_for_json(v)
            
            # Debug: Verify critical fields are present
            if 'hit_1' not in d:
                print(f"WARNING: Task {t.task_id[:8]} missing 'hit_1' in save! Current value: {getattr(t, 'hit_1', 'MISSING')}")
            
            serializable_data.append(d)
    
    # 🔧 ATOMIC SAVE with sanitization (removed default=str fallback)
    temp_path = filepath + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            # All data is pre-sanitized, no need for default=str
            json.dump(serializable_data, f, indent=2)
        os.replace(temp_path, filepath)
        return f"✅ Saved {len(tasks)} tasks to {filename}", filename
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)  # Delete broken temp file
        return f"❌ Save failed: {str(e)}", filename


# 3. Load tasks from selected JSON file (Optimized & Thread-Safe)
@app.callback(
    Output("save-load-status", "children", allow_duplicate=True),
    Output("task-ids-store", "data", allow_duplicate=True),
    Output("task-count-store", "data", allow_duplicate=True),
    Output("task-page-store", "data", allow_duplicate=True),
    Output("analysis-complete-trigger", "data", allow_duplicate=True), # 🔧 NEW
    Input("load-tasks-btn", "n_clicks"),
    State("json-file-select", "value"),
    prevent_initial_call=True
)
def load_tasks_from_json(n, filepath):
    if not filepath or not os.path.exists(filepath):
        return "⚠️ Please select a valid JSON file.", [], 0, 0, 0  # 🔧 Added 5th value (trigger=0)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return "❌ Invalid JSON format: expected a list of tasks.", [], 0, 0, 0  # 🔧 Added 5th value
    except json.JSONDecodeError as e:
        return f"❌ JSON Syntax Error at line {e.lineno}, col {e.colno}: {e.msg}.", [], 0, 0, 0  # 🔧 Added 5th value
    except Exception as e:
        return f"❌ Load failed: {str(e)}", [], 0, 0, 0  # 🔧 Added 5th value
        
    loaded_ids = []
    skipped = 0
    new_tasks = {}
    seen_ids = set()  # P3 IMPROVEMENT: Track unique task IDs
    
    # 🔧 DATETIME FIELDS that need restoration on load
    datetime_fields = {'start_date', 'end_date', 'first_event_time', 'max_adverse_time',
                       'max_expected_time', 'max_adverse_sgnl_time', 'max_expected_sgnl_time',
                       'max_adverse_before_return_time', 'max_adverse_before_return_sgnl_time',
                       'drawdown_before_level_time', 'drawdown_before_1pct_time', 
                       'drawdown_before_1_5pct_time', 'drawdown_before_2pct_time'}
    
    # 🔧 Use global _parse_timestamp for UTC-aware datetime parsing
    # This ensures all timestamps are converted to UTC-aware datetime objects
    
    for d in data:
        try:
            # P3 IMPROVEMENT: Check for duplicate task IDs
            task_id_candidate = d.get('task_id')
            if not task_id_candidate:
                print(f"Skipping task without task_id: {d}")
                skipped += 1
                continue
            if task_id_candidate in seen_ids:
                print(f"Duplicate task_id detected: {task_id_candidate}, skipping")
                skipped += 1
                continue
            seen_ids.add(task_id_candidate)
            
            # 1. Initialize Task with Core Attributes
            init_kwargs = {k: d.get(k) for k in ['task_id', 'symbols', 'timeframe', 'mode', 'start_date', 'end_date',
                'overwrite', 'price_continuity_check', 'signal_time', 'signal_price',
                'signal_symbol', 'signal_direction', 'analyze_beyond', 'enable_strategy',
                'enable_impulse', 'pre_buffer_minutes', 'log_events', 'hide_logs']}

            # Parse Datetimes for Init
            for k in datetime_fields:
                if k in init_kwargs and isinstance(init_kwargs[k], str):
                    init_kwargs[k] = _parse_timestamp(init_kwargs[k])

            task = DownloadTask(**init_kwargs)

            # 2. Restore ALL Other Attributes from JSON
            for k, v in d.items():
                if hasattr(task, k) and k not in init_kwargs:
                    try:
                        if k in datetime_fields:
                            setattr(task, k, _parse_timestamp(v))
                        elif k in ['signal_time', 'signal_price']:
                            setattr(task, k, float(v))
                        else:
                            setattr(task, k, v)
                    except Exception:
                        # If an attribute fails to restore, skip it silently (robustness)
                        pass 

            new_tasks[task.task_id] = task
            loaded_ids.append(task.task_id)
        except Exception as e:
            print(f"Error loading task: {e}")
            skipped += 1
            
    # 🔧 ATOMIC & THREAD-SAFE MEMORY UPDATE
    with tm.lock:
        tm.tasks.clear()
        tm.tasks.update(new_tasks)

    # 🔧 CRITICAL: Reset Version to Force Stats & Table Re-render
    # Since we split the callback, we just increment the version to trigger both new callbacks
    global golden_store_version
    golden_store_version += 1
        
    count = len(loaded_ids)
    msg = f"✅ Loaded {count} tasks from {os.path.basename(filepath)}"
    if skipped > 0:
        msg += f" | ⚠️ Skipped {skipped} corrupted tasks"
    # 🔧 Increment trigger to force UI refresh after load
    import time
    trigger_val = int(time.time()) 
    
    return msg, loaded_ids, count, 0, trigger_val

@app.callback(
    Output("save-load-status", "children", allow_duplicate=True),
    Output("task-ids-store", "data", allow_duplicate=True),
    Output("task-count-store", "data", allow_duplicate=True),
    Output("task-page-store", "data", allow_duplicate=True),
    Input("clear-all-tasks-btn", "n_clicks"),
    prevent_initial_call=True
)
def manual_clear_all(n):
    """Instantly wipes all tasks from RAM and resets UI stores."""
    global STOP_REQUESTED
    STOP_REQUESTED = True  # 🔧 Safely halt background recalc (sync with STOP_REQUESTED)
    recalc_bg["stop_flag"] = True  # 🔧 Also set recalc_bg flag for UI
    with tm.lock:
        tm.tasks.clear()
    return "🗑️ All tasks cleared.", [], 0, 0

@app.callback(
    Output("bulk-rerun-status", "children", allow_duplicate=True),
    Output("analysis-complete-trigger", "data", allow_duplicate=True), # 🔧 NEW
    Input("recalc-table-flags-btn", "n_clicks"),
    prevent_initial_call=True
)
def recalc_table_flags(n):
    """Recomputes ONLY the table column flags..."""
    global STOP_REQUESTED
    if not n: 
        return dash.no_update, dash.no_update  # 🔧 Return tuple
    
    # 🔧 CRITICAL: Reset stop flag before starting new recalculation
    STOP_REQUESTED = False
    
    if recalc_bg["running"]: 
        return "⏳ Recalculation already in progress...", dash.no_update  # 🔧 Return tuple
        
    tasks = [t for t in tm.get_all_tasks() if t.signal_time is not None and t.status == "completed"]
    if not tasks:
        return "⚠️ No completed tasks with signal data to recalc.", dash.no_update  # 🔧 Return tuple

    # 🔧 CRITICAL: Serialize tasks to dict format INSIDE the main thread (same logic as save_tasks_to_json)
    # This ensures all attributes are properly captured before passing to background thread
    import copy
    initial_tasks = []
    for t in tasks:
        d = {}
        for k, v in t.__dict__.items():
            # Skip non-serializable objects (locks, events, caches)
            if k in ('stop_event', 'pause_event', 'state_lock', 'raw_batches', '_chart_cache', 'symbol_ranges'):
                continue
            # Handle datetime objects
            if isinstance(v, (datetime, pd.Timestamp)):
                d[k] = v.isoformat()
            elif isinstance(v, (int, float, str, bool, type(None))):
                d[k] = v
            elif isinstance(v, (list, dict)):
                try:
                    json.dumps(v)
                    d[k] = v
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    d[k] = str(v)
                except Exception:
                    continue
        initial_tasks.append(d)
    
    # 🔧 CRITICAL: Set global counters
    global recalc_total_tasks, is_recalculating_flag, recalc_progress_count
    recalc_total_tasks = len(initial_tasks)
    is_recalculating_flag = True
    recalc_progress_count = 0
    
    # 🔧 CRITICAL: Update recalc_bg status BEFORE starting thread
    recalc_bg["running"] = True
    recalc_bg["total"] = len(initial_tasks)
    recalc_bg["count"] = 0
    recalc_bg["stop_flag"] = False  # 🔧 Reset stop flag in recalc_bg dict
    recalc_bg["trigger_val"] = 0  # 🔧 Reset trigger value
    
    # 🔧 CRITICAL: Enable the poller to monitor completion
    global recalc_poller_enabled
    recalc_poller_enabled = True
    
    # 🔧 CRITICAL: Start background thread passing initial_tasks as argument
    import threading
    threading.Thread(target=_run_recalc_background, args=(initial_tasks,), daemon=True).start()

    # 🔧 Increment trigger to force UI refresh after recalc starts
    import time
    trigger_val = int(time.time())

    return f"🔄 Recalculation started in background. Checking {len(tasks)} existing tasks...", trigger_val  # 🔧 Already correct

def _run_recalc_background(tasks_list):
    """Runs in background thread to never block the UI."""
    global recalc_progress_count, is_recalculating_flag, recalculation_complete_timestamp, current_tasks, STOP_REQUESTED, recalc_bg
    
    # 🔧 CRITICAL: Create LOCAL ALIASES for modules to avoid global lookup issues in threads
    import sys as _sys
    import bisect as _bisect
    import numpy as np
    import pandas as pd
    
    # Create module-level aliases accessible throughout this function
    sys = _sys
    bisect = _bisect
    
    # 🔧 CRITICAL: DO NOT clear parquet cache - we use cached data from RAM for fast analysis
    # The original design was to avoid re-reading files when analyzing JSON-loaded tasks
    
    # 🔧 HEARTBEAT: Confirm thread started
    print(f"🔥 [RECALC THREAD] Started with {len(tasks_list)} tasks")
    sys.stdout.flush()
    
    total_tasks = len(tasks_list)
    
    # 🔧 DYNAMIC STEP CALCULATOR: Ensures ~50 progress updates regardless of batch size
    # For 10 tasks: step = max(1, 10//50) = 1 → updates every task (10 updates)
    # For 89 tasks: step = max(1, 89//50) = 1 → updates every task (89 updates)
    # For 3500 tasks: step = max(1, 3500//50) = 70 → updates every 70 tasks (50 updates)
    step = max(1, total_tasks // 50)
    print(f"🔥 [RECALC THREAD] Dynamic step calculated: {step} (total={total_tasks})")
    sys.stdout.flush()
    
    # 🔧 DATETIME FIELDS that need restoration from ISO strings
    datetime_fields = {'start_date', 'end_date', 'first_event_time', 'max_adverse_time',
                       'max_expected_time', 'max_adverse_sgnl_time', 'max_expected_sgnl_time',
                       'max_adverse_before_return_time', 'max_adverse_before_return_sgnl_time',
                       'drawdown_before_level_time', 'drawdown_before_1pct_time', 
                       'drawdown_before_1_5pct_time', 'drawdown_before_2pct_time'}
    
    # 🔧 Use global _parse_timestamp for UTC-aware datetime parsing
    # (Defined at module level for consistency across save/load operations)
    
    # 🔧 TRACK SUCCESS/FAILURE COUNTS
    success_count = 0
    error_count = 0
    
    for i, t_dict in enumerate(tasks_list):
        # 🛑 PATCH A: Check for stop request every iteration (check both flags)
        if STOP_REQUESTED or recalc_bg.get("stop_flag", False):
            print(f"⚠️ [RECALC THREAD] Stop requested at {i}/{total_tasks}. Finishing safely...")
            sys.stdout.flush()
            break
            
        try:
            # 🔧 RECONSTRUCT TASK OBJECT FROM DICTIONARY
            # Get task from memory if it exists, otherwise create a new one from dict
            task_id = t_dict.get('task_id')
            task_symbol = t_dict.get('symbols', ['UNKNOWN'])[0] if isinstance(t_dict.get('symbols'), list) else 'UNKNOWN'
            task_tf = t_dict.get('timeframe', 'unknown')
            
            print(f"🔍 [TASK {i+1}/{total_tasks}] Starting: {task_symbol} {task_tf} (ID: {task_id})")
            sys.stdout.flush()
            
            task = tm.get_task(task_id) if task_id else None
            
            if task is None:
                # Reconstruct task from dictionary
                init_kwargs = {k: t_dict.get(k) for k in ['task_id', 'symbols', 'timeframe', 'mode', 'start_date', 'end_date',
                    'overwrite', 'price_continuity_check', 'signal_time', 'signal_price',
                    'signal_symbol', 'signal_direction', 'analyze_beyond', 'enable_strategy',
                    'enable_impulse', 'pre_buffer_minutes', 'log_events', 'hide_logs']}
                
                # Parse Datetimes
                for k in datetime_fields:
                    if k in init_kwargs and isinstance(init_kwargs[k], str):
                        init_kwargs[k] = _parse_timestamp(init_kwargs[k])
                
                task = DownloadTask(**init_kwargs)
                
                # Restore ALL Other Attributes from Dictionary
                for k, v in t_dict.items():
                    if hasattr(task, k) and k not in init_kwargs:
                        try:
                            if k in datetime_fields:
                                setattr(task, k, _parse_timestamp(v))
                            elif k in ['signal_time', 'signal_price']:
                                setattr(task, k, float(v))
                            else:
                                setattr(task, k, v)
                        except Exception:
                            pass
            
            # Now process the reconstructed task object
            if task.signal_time is not None and task.status == "completed":
                print(f"📊 [TASK {i+1}/{total_tasks}] Running analyze_signal for {task_symbol} {task_tf}...")
                sys.stdout.flush()
                
                # 🔧 CRITICAL: Acquire state_lock before modifying strategy signals
                with task.state_lock:
                    task.analyze_signal()  # This is the slow part
                    
                print(f"✅ [TASK {i+1}/{total_tasks}] Completed analyze_signal for {task_symbol} {task_tf}")
                sys.stdout.flush()
                
                # 🔧 CRITICAL: Auto-save recalculated tasks to persist new data
                task.add_log("💾 Recalculation complete - data updated in memory")
                success_count += 1  # ✅ Track successful recalculation
            else:
                print(f"⏭️ [TASK {i+1}/{total_tasks}] Skipping (no signal_time or not completed): {task_symbol} {task_tf}")
                sys.stdout.flush()
                # Skipped tasks don't count as errors or successes
        except Exception as e:
            # ⚠️ WARNING ONLY: Continue processing even if task has errors (old Mac safe)
            import traceback
            print(f"❌ [TASK {i+1}/{total_tasks}] ERROR on {task_symbol if 'task_symbol' in locals() else 'UNKNOWN'} {task_tf if 'task_tf' in locals() else 'unknown'}: {e}")
            traceback.print_exc()
            sys.stdout.flush()
            error_count += 1  # ❌ Track failed recalculation
            try: 
                if task:
                    task.add_log(f"⚠️ Recalc error: {e}")
            except: pass

        # 🔧 CRITICAL: Update progress counter with DYNAMIC STEP for any batch size
        # This prevents freezing where small task counts would never reach the update threshold
        if (i + 1) % step == 0 or (i + 1) == total_tasks:
            recalc_progress_count = i + 1
            recalc_bg["count"] = i + 1  # 🔧 Update recalc_bg for UI polling
            print(f"🔥 [RECALC THREAD] Progress: {i + 1}/{total_tasks} (step={step})")
            sys.stdout.flush()
            
        # 🔧 HEARTBEAT: Every 10 seconds, print a heartbeat to confirm thread is alive
        if (i + 1) % max(10, step) == 0:
            print(f"💓 [RECALC THREAD] Heartbeat: Processing task {i + 1}/{total_tasks}...")
            sys.stdout.flush()

    # 🔧 CRITICAL: Update global RAM with processed tasks (atomic swap)
    with tm.lock:
        # Tasks were modified in-place during the loop, so they're already in tm.tasks
        # Just ensure current_tasks reflects the latest state
        current_tasks = list(tm.tasks.values())
    
    # 🔧 GOLDEN STORE: Populate pre-processed cache for instant pagination
    global golden_task_store_data, golden_store_version
    with tm.lock:
        golden_task_store_data = list(tm.tasks.values())
        golden_store_version += 1  # Increment version to invalidate page caches

    # 🔧 RECALC LOCK: Release lock to allow UI interaction
    global recalc_lock
    recalc_lock = {"locked": False, "message": "Recalculation complete"}
    
    # 🔧 CRITICAL: Update flags and timestamp (NO Auto-Save - user must press Save button)
    recalculation_complete_timestamp = time.time()
    is_recalculating_flag = False
    STOP_REQUESTED = False  # Reset stop flag for next run
    final_count = i + 1 if STOP_REQUESTED else total_tasks
    recalc_progress_count = final_count
    recalc_bg["count"] = final_count  # 🔧 Final count update
    recalc_bg["running"] = False  # 🔧 Signal completion to UI
    recalc_bg["trigger_val"] = int(time.time() * 1000)  # 🔧 NEW: Store trigger value for polling
    
    # 🔧 CRITICAL: Increment trigger to force UI refresh AFTER recalculation completes
    # This ensures task table and summary table show the updated data
    analysis_trigger_val = int(time.time() * 1000)  # Use milliseconds to ensure unique value

    if STOP_REQUESTED:
        print(f"⚠️ [RECALC THREAD] Recalculation stopped early: {final_count}/{total_tasks} tasks processed")
    elif error_count > 0:
        # 🚨 HONEST REPORTING: Show errors prominently
        print(f"🔴 [RECALC THREAD] Recalculation completed with ERRORS: {success_count} succeeded, {error_count} failed out of {total_tasks} tasks. FIX ERRORS before saving!")
    elif success_count == 0:
        # 🚨 HONEST REPORTING: No tasks were actually recalculated
        print(f"🔴 [RECALC THREAD] Recalculation completed but NOTHING WAS UPDATED: 0/{total_tasks} tasks recalculated. Check task status and signal data!")
    else:
        # ✅ Calculate how many tasks were skipped (no signal_time or not completed)
        skipped_count = total_tasks - success_count - error_count
        if skipped_count > 0:
            print(f"✅ [RECALC THREAD] Recalculation successful: {success_count}/{total_tasks} tasks updated.")
            print(f"ℹ️ [RECALC THREAD] Note: {skipped_count} task(s) were skipped (no signal time or incomplete status).")
            print(f"💾 [RECALC THREAD] Results in RAM - press 'Save New JSON' to persist.")
        else:
            print(f"✅ [RECALC THREAD] Recalculation successful: {success_count}/{total_tasks} tasks updated. Results in RAM - press 'Save New JSON' to persist.")
    sys.stdout.flush()
    
    # 🔧 CRITICAL: Return the trigger value so callback can update the store
    return analysis_trigger_val


@app.callback(
    Output("recalc-status-bar", "children"),
    Input("recalc-status-interval", "n_intervals"),
    prevent_initial_call=False
)
def update_status_bar(n):
    """Real-time status bar callback triggered every 1 second."""
    if is_recalculating_flag:
        # 🔧 FIX: Use recalc_bg["count"] for real-time progress instead of recalc_progress_count
        # which only updates in batches and can appear frozen
        current_count = recalc_bg.get("count", 0) if recalc_bg.get("running", False) else recalc_progress_count
        return f"⚙️ Checking: {current_count} / {recalc_total_tasks} tasks..."
    else:
        return "Ready"

@app.callback(
    Output("bulk-rerun-status", "children", allow_duplicate=True),
    Output("analysis-complete-trigger", "data", allow_duplicate=True), # 🔧 NEW: Also update trigger when polling detects completion
    Input("progress-interval", "n_intervals"),
    prevent_initial_call=True
)
def poll_recalc_progress(_):
    if not recalc_bg["running"]:
        # 🔧 FIX: Return a completion message instead of no_update
        # This ensures the UI shows "Done" instead of getting stuck on the last progress count
        if recalc_bg["total"] > 0:
            # 🔧 CRITICAL: Check if we have a trigger value from completed recalculation
            trigger_val = recalc_bg.get("trigger_val", 0)
            if trigger_val > 0:
                return f"✅ Recalculation complete. ({recalc_bg['count']}/{recalc_bg['total']} tasks updated)", trigger_val
            return f"✅ Recalculation complete. ({recalc_bg['count']}/{recalc_bg['total']} tasks updated)", dash.no_update
        else:
            return no_update, dash.no_update
    return f"⏳ Recalculating... {recalc_bg['count']}/{recalc_bg['total']} completed", dash.no_update

# 🔧 NEW: Dedicated poller for triggering UI refresh after recalculation completes
@app.callback(
    Output("recalc-poller", "disabled"),
    Output("analysis-complete-trigger", "data", allow_duplicate=True),
    Input("recalc-poller", "n_intervals"),
    State("recalc-poller", "disabled"),
    prevent_initial_call=True
)
def trigger_ui_on_recalc_complete(n_intervals, is_disabled):
    """Polls every 1 second during recalculation and triggers UI refresh when complete."""
    global recalc_poller_enabled
    
    # Check if recalculation just finished
    if not recalc_bg["running"] and recalc_poller_enabled:
        # Recalculation just finished - trigger UI refresh
        trigger_val = recalc_bg.get("trigger_val", int(time.time() * 1000))
        print(f"🔥 [UI POLLER] Recalculation complete! Triggering UI refresh with value: {trigger_val}")
        # Reset poller state
        recalc_poller_enabled = False
        # Enable (disable=True) the poller until next recalculation
        return True, trigger_val
    elif recalc_bg["running"] and not recalc_poller_enabled:
        # Recalculation started - keep poller enabled (disabled=False)
        recalc_poller_enabled = True
        return False, dash.no_update
    # Keep current state
    return dash.no_update, dash.no_update

if __name__ == "__main__":
    app.run(debug=True, port=8050)
