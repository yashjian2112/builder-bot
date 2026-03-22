"""
Conversation manager — the AI brain of the bot.

Uses Claude claude-opus-4-6 with adaptive thinking to drive the user through:
  1. Requirements discovery (brainstorming + cross-questions)
  2. Existing code analysis (if applicable)
  3. UI/UX mockup generation
  4. Task planning
  5. Execution oversight
"""

from __future__ import annotations

import json
import re
from typing import Optional

import anthropic

# ─── System prompt ───────────────────────────────────────

SYSTEM_PROMPT = """You are "Builder Bot" — an elite AI project manager and software architect.
You guide users from idea → requirements → UI design → working software.

━━━ YOUR PHASES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 1 — DISCOVERY
Ask smart, targeted questions to fully understand the project.
Ask ONE question at a time. Wait for the answer before asking the next.
Typical areas to cover:
  • Is this a new app or enhancement to existing one?
  • What does the business do? What industry?
  • Who will use this? (internal team, customers, admins?)
  • What core problems does it solve?
  • What are the MUST-HAVE features? Nice-to-haves?
  • Any existing systems to integrate with?
  • Tech preferences? (language, framework, database)
  • Rough scale? (10 users vs 10,000 users)
  • Any deadline or constraints?

When you feel you have a COMPLETE picture (typically 6-10 exchanges), output:
<REQUIREMENTS_DONE>
{
  "project_name": "...",
  "project_type": "new",
  "business_context": "...",
  "target_users": "...",
  "core_features": ["feature 1", "feature 2", ...],
  "nice_to_have": ["..."],
  "tech_stack": {
    "language": "...",
    "framework": "...",
    "database": "...",
    "other": [...]
  },
  "integrations": [...],
  "constraints": [...],
  "scope": "small|medium|large"
}
</REQUIREMENTS_DONE>

PHASE 1b — EXISTING CODE ANALYSIS
If the user has an existing project, tell them:
  "Please provide the folder path to your existing project so I can analyze it."
Output: <NEED_FOLDER_PATH>

After analysis data is provided, ask targeted questions based on what you found.
When done, proceed to REQUIREMENTS_DONE.

PHASE 2 — UI/UX DESIGN
After requirements are confirmed, create a comprehensive HTML/CSS mockup.
The mockup MUST include:
  • Navigation / sidebar
  • All major screens (you can use tab-switching to show multiple views)
  • Real data examples (not placeholder text if possible)
  • Professional color scheme matching the business context
  • Responsive layout

Output the COMPLETE mockup HTML as:
<UI_MOCKUP>
<!DOCTYPE html>
[complete self-contained HTML with embedded CSS and JS]
</UI_MOCKUP>

After showing the mockup, ask if they want any changes.

PHASE 3 — TASK PLANNING
After UI is approved, create a detailed, ordered implementation plan.
Each task must be ATOMIC (one concern), with enough detail that a developer
can implement it without asking questions.

Output:
<TASK_PLAN>
[
  {
    "id": "T01",
    "title": "...",
    "type": "backend|frontend|database|devops|testing",
    "description": "Detailed description of what to build",
    "technical_details": "Specific implementation details, function signatures, DB schema, API endpoints, etc.",
    "files_to_create": ["path/to/file.py", ...],
    "dependencies": [],
    "estimated_complexity": "low|medium|high"
  }
]
</TASK_PLAN>

PHASE 4 — EXECUTION
Once tasks are approved and running, monitor progress.
If a task needs clarification, ask the user clearly.
Report progress updates naturally.

━━━ IMPORTANT RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Be warm, professional, and encouraging
• Ask ONE question at a time — never bombard with multiple questions
• Always acknowledge what the user said before moving on
• If something is unclear, ask for clarification rather than assuming
• Share your expert opinions and suggestions proactively
• For existing apps: highlight what's good and what can be improved
• The UI mockup should look PROFESSIONAL — use good typography, spacing, color
"""


# ─── Phase detection ─────────────────────────────────────

def extract_block(tag: str, text: str) -> Optional[str]:
    """Extract content between <TAG> and </TAG>."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else None


def detect_signals(text: str) -> dict:
    """Scan Claude's reply for special output markers."""
    signals: dict = {}

    req_json = extract_block("REQUIREMENTS_DONE", text)
    if req_json:
        try:
            signals["requirements"] = json.loads(req_json)
        except json.JSONDecodeError:
            signals["requirements_raw"] = req_json

    if "<NEED_FOLDER_PATH>" in text:
        signals["need_folder"] = True

    mockup_html = extract_block("UI_MOCKUP", text)
    if mockup_html:
        signals["mockup_html"] = mockup_html

    task_json = extract_block("TASK_PLAN", text)
    if task_json:
        try:
            signals["task_plan"] = json.loads(task_json)
        except json.JSONDecodeError:
            signals["task_plan_raw"] = task_json

    return signals


def clean_reply(text: str) -> str:
    """Remove special marker blocks from the visible reply."""
    for tag in ["REQUIREMENTS_DONE", "UI_MOCKUP", "TASK_PLAN"]:
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
    text = text.replace("<NEED_FOLDER_PATH>", "")
    return text.strip()


# ─── ConversationManager ─────────────────────────────────

class ConversationManager:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def chat(
        self,
        session_id: str,
        history: list[dict],
        user_message: str,
        extra_context: str = "",
        phase: str = "greeting",
    ) -> dict:
        """
        Send a user message and get the bot's response.

        Returns:
          {
            "reply":        str,          # clean text to show user
            "signals":      dict,         # detected phase transitions
            "raw":          str,          # full raw response
          }
        """
        system = SYSTEM_PROMPT
        if extra_context:
            system += f"\n\n━━━ ADDITIONAL CONTEXT ━━━\n{extra_context}"
        if phase:
            system += f"\n\n[Current phase: {phase}]"

        messages = list(history)
        messages.append({"role": "user", "content": user_message})

        response = self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8192,
            thinking={"type": "adaptive"},
            system=system,
            messages=messages,
        )

        raw = next((b.text for b in response.content if b.type == "text"), "")
        signals = detect_signals(raw)
        reply = clean_reply(raw)

        return {
            "reply":   reply,
            "signals": signals,
            "raw":     raw,
        }

    def generate_context_document(
        self,
        requirements: dict,
        task_plan: list,
        task_index: int,
        project_dir: str,
        code_analysis: str = "",
        completed_tasks: list = None,
    ) -> str:
        """
        Build the MEGA context document passed to Claude Code for each task.
        This ensures Claude Code has zero ambiguity.
        """
        completed = completed_tasks or []
        current_task = task_plan[task_index]

        parts = [
            "# PROJECT CONTEXT DOCUMENT",
            "# This document gives complete context for implementing the current task.",
            "# Read everything carefully before writing any code.\n",

            "## PROJECT OVERVIEW",
            f"**Name:** {requirements.get('project_name', 'Project')}",
            f"**Type:** {requirements.get('project_type', 'new')}",
            f"**Business:** {requirements.get('business_context', '')}",
            f"**Target Users:** {requirements.get('target_users', '')}",
            "",

            "## TECH STACK",
            json.dumps(requirements.get("tech_stack", {}), indent=2),
            "",

            "## CORE FEATURES",
        ]
        for feat in requirements.get("core_features", []):
            parts.append(f"  - {feat}")

        parts += [
            "",
            "## CONSTRAINTS & REQUIREMENTS",
        ]
        for c in requirements.get("constraints", []):
            parts.append(f"  - {c}")

        parts += [
            "",
            "## PROJECT DIRECTORY",
            f"{project_dir}",
            "",
            "## ALL TASKS (for full context)",
        ]
        for i, t in enumerate(task_plan):
            status = "CURRENT" if i == task_index else ("DONE" if t["id"] in completed else "PENDING")
            parts.append(f"\n### [{status}] {t['id']}: {t['title']}")
            parts.append(f"Type: {t['type']}")
            parts.append(f"Description: {t['description']}")
            if t.get("technical_details"):
                parts.append(f"Technical details: {t['technical_details']}")
            if t.get("files_to_create"):
                parts.append(f"Files: {', '.join(t['files_to_create'])}")

        parts += [
            "",
            "=" * 60,
            "## CURRENT TASK TO IMPLEMENT",
            f"**ID:** {current_task['id']}",
            f"**Title:** {current_task['title']}",
            f"**Type:** {current_task['type']}",
            f"**Description:** {current_task['description']}",
        ]

        if current_task.get("technical_details"):
            parts.append(f"\n**Technical Details:**\n{current_task['technical_details']}")

        if current_task.get("files_to_create"):
            parts.append("\n**Files to create/modify:**")
            for f in current_task["files_to_create"]:
                parts.append(f"  - {project_dir}/{f}")

        parts += [
            "",
            "## YOUR INSTRUCTIONS",
            "1. Implement this task completely and correctly.",
            "2. Create all files listed above.",
            "3. Write clean, production-quality code with comments.",
            "4. Handle errors properly.",
            "5. After writing files, run a quick verification (e.g., python -c 'import module').",
            "6. Do NOT ask for permission — you have full file access.",
            "7. If something is unclear, make the best decision and leave a TODO comment.",
        ]

        if code_analysis:
            parts += [
                "",
                "=" * 60,
                "## EXISTING CODEBASE CONTEXT",
                code_analysis,
            ]

        return "\n".join(parts)
