---
name: clickable-dashboard-launch
description: Added one-click Windows batch/PowerShell scripts for bot start/stop with automatic web dashboard launch
metadata:
  type: project
---

Enhanced the Roostoo trading bot with clickable Windows scripts that:

1. start_bot.bat/start_bot.ps1:
   - Launches bot in new console window
   - Automatically opens web dashboard at http://localhost:8080 after 3-second delay
   - Shows clear startup status messages
   - Properly detects Python from venv or system PATH
   - Prevents duplicate instance startup

2. stop_bot.bat/stop_bot.ps1:
   - Gracefully shuts down bot via REST command and sentinel file
   - 10-second countdown with force-kill/cancel options
   - Automatically opens dashboard post-shutdown for state verification
   - Cleans up PID and shutdown trigger files
   - Verifies portfolio/audit trail persistence

3. Documentation updates:
   - Added One-Click Windows Launch section to README.md
   - Added Section 5 to docs/OPERATIONS_GUIDE.md with script details
   - All scripts require zero terminal commands - just double-click

These improvements make bot operation much more user-friendly for Windows users while maintaining all the graceful shutdown and state persistence features.
**Why:** Users previously needed to run terminal commands to start/stop the bot and manually open the dashboard. This adds true double-click usability.
**How to apply:** Double-click start_bot.bat to launch bot + dashboard, stop_bot.bat to gracefully shut down and verify state.