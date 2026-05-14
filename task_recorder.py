#!/usr/bin/env python3
"""终端任务记录器 - 轻量级多实例任务管理工具，支持自动检测 Claude Code 会话"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime

# Windows 终端 UTF-8 支持
if sys.platform == "win32":
    os.system("")  # 启用 ANSI 转义序列
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.theme import Theme

# ─── 主题配色 ──────────────────────────────────────────────────────────────────

CUSTOM_THEME = Theme({
    "header":       "bold #c0caf5",        # 淡蓝白 - 标题
    "accent":       "#7aa2f7",             # 柔蓝 - 强调
    "muted":        "#565f89",             # 灰紫 - 次要文字
    "ok":           "#9ece6a",             # 柔绿 - 成功
    "warn":         "#e0af68",             # 暖黄 - 警告
    "err":          "#f7768e",             # 柔红 - 错误
    "info":         "#7dcfff",             # 亮青 - 信息
})

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.db")
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
console = Console(force_terminal=True, theme=CUSTOM_THEME)

VALID_STATUSES = ("pending", "working", "done", "failed")
VALID_PRIORITIES = ("low", "medium", "high")

STATUS_STYLES = {
    "pending":  Style(color="#e0af68"),                    # 暖黄
    "working":  Style(color="#7aa2f7", bold=True),         # 柔蓝+粗
    "done":     Style(color="#565f89", strike=True),       # 灰色+删除线
    "failed":   Style(color="#f7768e"),                    # 柔红
}

STATUS_ICONS = {
    "pending":  "[#e0af68]--[/]",
    "working":  "[bold #7aa2f7]>>[/]",
    "done":     "[#565f89]ok[/]",
    "failed":   "[#f7768e]!![/]",
}

PRIORITY_STYLES = {
    "low":    Style(color="#565f89"),           # 灰
    "medium": Style(color="#c0caf5"),           # 淡白
    "high":   Style(color="#f7768e", bold=True),# 红+粗
}

SESSION_STYLES = {
    "active": Style(color="#9ece6a", bold=True),  # 绿
    "idle":   Style(color="#e0af68"),              # 黄
    "stale":  Style(color="#565f89"),              # 灰
}

SESSION_LABELS = {
    "active": "LIVE",
    "idle":   "IDLE",
    "stale":  "---",
}

TABLE_BORDER = Style(color="#3b4261")  # 暗灰蓝边框
TABLE_TITLE  = Style(color="#c0caf5", bold=True)


# ─── SQLite ────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority TEXT NOT NULL DEFAULT 'medium',
            notes TEXT NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at DATETIME NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    return conn


def get_task_or_exit(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        console.print(f"[err]错误: 任务 #{task_id} 不存在[/]")
        raise SystemExit(1)
    return row


def update_status(task_id, new_status):
    conn = get_db()
    task = get_task_or_exit(conn, task_id)
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (new_status, task_id),
    )
    conn.commit()
    conn.close()
    old_s = Text(task["status"], style=STATUS_STYLES.get(task["status"]))
    new_s = Text(new_status, style=STATUS_STYLES.get(new_status))
    console.print("  ", Text(f"#{task_id} ", style="accent"), old_s, " -> ", new_s)


# ─── Claude Code 会话扫描 ──────────────────────────────────────────────────────

def _project_name_from_dir(dirname):
    name = dirname.replace("--", ":/").replace("-", "/")
    segments = name.replace("\\", "/").split("/")
    if len(segments) > 2:
        return "/".join(segments[-2:])
    return name


def _extract_first_user_message(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    texts = [c.get("text", "") for c in content if isinstance(c, dict)]
                    content = " ".join(texts)
                content = content.strip()
                if not content or content.startswith("[Request interrupted"):
                    continue
                first_line = content.split("\n")[0].strip()
                if len(first_line) > 80:
                    first_line = first_line[:77] + "..."
                return first_line, obj.get("slug", ""), obj.get("sessionId", "")
    except (OSError, UnicodeDecodeError):
        pass
    return None, None, None


def scan_claude_sessions(minutes=30):
    if not os.path.isdir(CLAUDE_DIR):
        return []

    sessions = []
    now = time.time()
    cutoff = now - minutes * 60

    for project_dir in os.listdir(CLAUDE_DIR):
        full_dir = os.path.join(CLAUDE_DIR, project_dir)
        if not os.path.isdir(full_dir):
            continue
        for fname in os.listdir(full_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(full_dir, fname)
            if "subagents" in fpath:
                continue
            mtime = os.path.getmtime(fpath)
            if mtime < cutoff:
                continue

            desc, slug, session_id = _extract_first_user_message(fpath)
            if not desc:
                continue

            age_sec = now - mtime
            if age_sec < 120:
                status = "active"
            elif age_sec < 600:
                status = "idle"
            else:
                status = "stale"

            sessions.append({
                "session_id": session_id or fname.replace(".jsonl", ""),
                "slug": slug or "",
                "project": _project_name_from_dir(project_dir),
                "description": desc,
                "status": status,
                "last_active": datetime.fromtimestamp(mtime),
                "age_sec": age_sec,
            })

    sessions.sort(key=lambda s: s["age_sec"])
    return sessions


# ─── 表格构建 ──────────────────────────────────────────────────────────────────

def _build_session_table(sessions):
    table = Table(
        title="Claude Code Sessions",
        title_style=TABLE_TITLE,
        border_style=TABLE_BORDER,
        show_lines=False,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", style="#565f89", justify="right", width=3)
    table.add_column("Session", style="#7aa2f7", width=16, no_wrap=True)
    table.add_column("Project", style="#9ece6a", width=18, no_wrap=True)
    table.add_column("Task", style="#c0caf5", ratio=1)
    table.add_column("Status", justify="center", width=6)
    table.add_column("Last", style="#565f89", justify="right", width=8, no_wrap=True)

    for i, s in enumerate(sessions, 1):
        status_text = Text(SESSION_LABELS.get(s["status"], "?"))
        status_text.stylize(SESSION_STYLES.get(s["status"]))

        age = s["age_sec"]
        if age < 60:
            age_str = f"{int(age)}s"
        elif age < 3600:
            age_str = f"{int(age / 60)}m"
        else:
            age_str = f"{int(age / 3600)}h"

        table.add_row(
            str(i),
            s["slug"][:16] if s["slug"] else "-",
            s["project"],
            s["description"],
            status_text,
            age_str,
        )

    return table


def _build_manual_table(all_statuses=False):
    conn = get_db()
    if all_statuses:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'working') ORDER BY id"
        ).fetchall()
    conn.close()

    if not rows:
        return None

    table = Table(
        title="Manual Tasks",
        title_style=TABLE_TITLE,
        border_style=TABLE_BORDER,
        show_lines=False,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("ID", style="#565f89", justify="right", width=4)
    table.add_column("Agent", style="#7aa2f7", width=12, no_wrap=True)
    table.add_column("Task", style="#c0caf5", ratio=1)
    table.add_column("Status", justify="center", width=9)
    table.add_column("Priority", justify="center", width=8)
    table.add_column("Notes", style="#565f89", width=20)

    for row in rows:
        icon = STATUS_ICONS.get(row["status"], "")
        status_text = Text(row["status"], style=STATUS_STYLES.get(row["status"]))
        priority_text = Text(row["priority"], style=PRIORITY_STYLES.get(row["priority"]))
        notes = row["notes"]
        if len(notes) > 18:
            notes = notes[:18] + "..."

        table.add_row(
            str(row["id"]),
            row["agent"] or "-",
            row["description"],
            status_text,
            priority_text,
            notes or "-",
        )

    return table


def _build_full_list_table():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    conn.close()

    if not rows:
        return None

    table = Table(
        title="All Tasks",
        title_style=TABLE_TITLE,
        border_style=TABLE_BORDER,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("ID", style="#565f89", justify="right", width=4)
    table.add_column("Agent", style="#7aa2f7", width=12, no_wrap=True)
    table.add_column("Task", style="#c0caf5", width=30)
    table.add_column("Status", justify="center", width=9)
    table.add_column("Priority", justify="center", width=8)
    table.add_column("Notes", style="#565f89", width=24)
    table.add_column("Created", style="#3b4261", width=16)

    for row in rows:
        status_text = Text(row["status"], style=STATUS_STYLES.get(row["status"]))
        priority_text = Text(row["priority"], style=PRIORITY_STYLES.get(row["priority"]))
        notes = row["notes"]
        if len(notes) > 22:
            notes = notes[:22] + "..."

        table.add_row(
            str(row["id"]),
            row["agent"] or "-",
            row["description"],
            status_text,
            priority_text,
            notes or "-",
            row["created_at"][:16],
        )

    return table


# ─── CLI 命令 ──────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """终端任务记录器 - 管理多实例并行任务"""
    pass


@cli.command()
@click.option("--minutes", "-m", default=60, help="扫描最近 N 分钟内活跃的会话")
@click.option("--refresh", "-r", default=5, help="刷新间隔(秒)")
def watch(minutes, refresh):
    """实时监控 Claude Code 会话 + 手动任务 (Ctrl+C 退出)"""
    try:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                parts = []

                # 标题
                now_str = datetime.now().strftime("%H:%M:%S")
                title = Text()
                title.append("  Task Recorder  ", style="bold #c0caf5")
                title.append(f"  {now_str}  ", style="#565f89")
                parts.append(title)
                parts.append(Text())

                # Claude 会话
                sessions = scan_claude_sessions(minutes)
                if sessions:
                    parts.append(_build_session_table(sessions))
                else:
                    parts.append(
                        Panel(
                            "[#565f89]No active Claude Code sessions detected[/]",
                            title="[#565f89]Sessions[/]",
                            border_style="#3b4261",
                            padding=(0, 2),
                        )
                    )

                parts.append(Text())

                # 手动任务
                manual = _build_manual_table()
                if manual:
                    parts.append(manual)

                # 底部
                active = sum(1 for s in sessions if s["status"] == "active") if sessions else 0
                idle = sum(1 for s in sessions if s["status"] == "idle") if sessions else 0
                bar = Text()
                bar.append(f"\n  {len(sessions)} sessions", style="#565f89")
                bar.append(f"  |  ", style="#3b4261")
                bar.append(f"{active} live", style="#9ece6a" if active else "#565f89")
                bar.append(f"  |  ", style="#3b4261")
                bar.append(f"{idle} idle", style="#e0af68" if idle else "#565f89")
                bar.append(f"  |  ", style="#3b4261")
                bar.append(f"Ctrl+C exit", style="#3b4261")
                parts.append(bar)

                live.update(Group(*parts))
                time.sleep(refresh)
    except KeyboardInterrupt:
        console.print("[muted]Bye.[/]")


@cli.command()
@click.option("--minutes", "-m", default=60, help="扫描最近 N 分钟内活跃的会话")
def scan(minutes):
    """扫描并显示当前 Claude Code 会话 (单次)"""
    sessions = scan_claude_sessions(minutes)
    if not sessions:
        console.print("[muted]No active sessions.[/]")
        return
    console.print(_build_session_table(sessions))


@cli.command()
@click.argument("description")
@click.option("--agent", "-a", default="", help="执行者名称")
@click.option("--priority", "-p", type=click.Choice(VALID_PRIORITIES), default="medium", help="优先级")
def add(description, agent, priority):
    """添加新任务"""
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO tasks (agent, description, priority) VALUES (?, ?, ?)",
        (agent, description, priority),
    )
    conn.commit()
    task_id = cursor.lastrowid
    conn.close()
    console.print(f"  [ok]+[/] [accent]#{task_id}[/] {description}")


@cli.command("list")
@click.option("--status", "-s", type=click.Choice(VALID_STATUSES), help="按状态筛选")
@click.option("--agent", "-a", default=None, help="按执行者筛选")
def list_tasks(status, agent):
    """查看任务列表"""
    conn = get_db()
    query = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if agent:
        query += " AND agent = ?"
        params.append(agent)
    query += " ORDER BY id"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        console.print("[muted]  No tasks.[/]")
        return

    table = Table(
        title="All Tasks",
        title_style=TABLE_TITLE,
        border_style=TABLE_BORDER,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("ID", style="#565f89", justify="right", width=4)
    table.add_column("Agent", style="#7aa2f7", width=12, no_wrap=True)
    table.add_column("Task", style="#c0caf5", width=30)
    table.add_column("Status", justify="center", width=9)
    table.add_column("Priority", justify="center", width=8)
    table.add_column("Notes", style="#565f89", width=24)
    table.add_column("Created", style="#3b4261", width=16)

    for row in rows:
        status_text = Text(row["status"], style=STATUS_STYLES.get(row["status"]))
        priority_text = Text(row["priority"], style=PRIORITY_STYLES.get(row["priority"]))
        notes = row["notes"]
        if len(notes) > 22:
            notes = notes[:22] + "..."

        table.add_row(
            str(row["id"]),
            row["agent"] or "-",
            row["description"],
            status_text,
            priority_text,
            notes or "-",
            row["created_at"][:16],
        )

    console.print(table)


@cli.command()
@click.argument("task_id", type=int)
def start(task_id):
    """标记任务为 working"""
    update_status(task_id, "working")


@cli.command()
@click.argument("task_id", type=int)
def done(task_id):
    """标记任务为 done"""
    update_status(task_id, "done")


@cli.command()
@click.argument("task_id", type=int)
def fail(task_id):
    """标记任务为 failed"""
    update_status(task_id, "failed")


@cli.command()
@click.argument("task_id", type=int)
@click.argument("text")
def note(task_id, text):
    """给任务追加备注"""
    conn = get_db()
    task = get_task_or_exit(conn, task_id)
    existing = task["notes"]
    new_notes = f"{existing}\n{text}".strip() if existing else text
    conn.execute(
        "UPDATE tasks SET notes = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (new_notes, task_id),
    )
    conn.commit()
    conn.close()
    console.print(f"  [ok]+[/] note added to [accent]#{task_id}[/]")


@cli.command()
@click.argument("task_id", type=int)
def rm(task_id):
    """删除任务"""
    conn = get_db()
    get_task_or_exit(conn, task_id)
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    console.print(f"  [err]-[/] removed [accent]#{task_id}[/]")


@cli.command()
def clean():
    """清理所有已完成(done)的任务"""
    conn = get_db()
    cursor = conn.execute("DELETE FROM tasks WHERE status = 'done'")
    count = cursor.rowcount
    conn.commit()
    conn.close()
    console.print(f"  [ok]+[/] cleaned {count} done tasks")


@cli.command()
def stats():
    """查看任务统计"""
    conn = get_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()

    if total == 0:
        console.print("[muted]  No tasks.[/]")
        return

    table = Table(
        title="Stats",
        title_style=TABLE_TITLE,
        border_style=TABLE_BORDER,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("Status", justify="center", width=10)
    table.add_column("Count", justify="right", width=6)

    counts = {row["status"]: row["cnt"] for row in rows}
    for s in VALID_STATUSES:
        cnt = counts.get(s, 0)
        table.add_row(
            Text(s, style=STATUS_STYLES.get(s)),
            Text(str(cnt), style="#c0caf5" if cnt else "#3b4261"),
        )

    table.add_row(
        Text("total", style="bold #c0caf5"),
        Text(str(total), style="bold #c0caf5"),
        style=Style(overline=True),
    )
    console.print(table)


if __name__ == "__main__":
    cli()
