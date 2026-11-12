"""Optional current-user Task Scheduler integration; never installs a system service."""
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import compat

TASK_NAME = "AI Agents Orchestrator"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def task_xml(cfg, config_file: Path, *, python=None, sid=None) -> str:
    root = ET.Element("Task", version="1.2", xmlns=NS)
    def add(parent, tag, text=None, **attributes):
        node = ET.SubElement(parent, tag, attributes)
        node.text = text
        return node
    identity = sid or compat.windows_sid()
    triggers = add(root, "Triggers")
    trigger = add(triggers, "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", identity)
    principal = add(add(root, "Principals"), "Principal", id="User")
    add(principal, "UserId", identity)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(root, "Settings")
    add(settings, "MultipleInstancesPolicy", "IgnoreNew")
    add(settings, "DisallowStartIfOnBatteries", "false")
    add(settings, "StopIfGoingOnBatteries", "false")
    add(settings, "ExecutionTimeLimit", "PT0S")
    action = add(add(root, "Actions", Context="User"), "Exec")
    add(action, "Command", python or sys.executable)
    add(action, "Arguments", subprocess.list2cmdline([
        "-X", "utf8", "-m", "ai_agents_orchestrator", "--config", str(config_file), "serve"]))
    add(action, "WorkingDirectory", str(config_file.parent))
    return ET.tostring(root, encoding="unicode")


def manage(cfg, action, config_file) -> int:
    if not compat.WINDOWS or cfg.mode != "lite":
        print("service --windows requires Windows native lite mode")
        return 2
    try:
        name = f"{TASK_NAME}-{compat.windows_sid()}"
        if action == "uninstall":
            cmd = [compat.system_tool("schtasks.exe"), "/Delete", "/TN", name, "/F"]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
        else:
            if not config_file.is_file():
                print("Run aao setup first; no user config file found")
                return 2
            # A private, temporary XML avoids nested shell /TR quoting and stores no password.
            with tempfile.TemporaryDirectory(prefix="aao-task-") as directory:
                xml = Path(directory) / "task.xml"
                xml.write_text(task_xml(cfg, config_file.resolve()), encoding="utf-16")
                result = subprocess.run([compat.system_tool("schtasks.exe"), "/Create", "/TN", name, "/XML", str(xml), "/F"],
                                        capture_output=True, timeout=30)
        if result.returncode:
            print("Task Scheduler rejected the request. Check your user's task permissions; no elevation attempted.")
            return 1
        print("Logon task installed; starts at your next logon. Use aao serve to start now." if action == "install"
              else "Logon task removed; an already running service is not stopped.")
        return 0
    except (OSError, subprocess.TimeoutExpired):
        print("Task Scheduler unavailable; run aao serve in a user terminal instead.")
        return 1
