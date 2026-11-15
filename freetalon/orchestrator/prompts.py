"""Centralized prompt contracts for the orchestrator LLM flows."""

from __future__ import annotations

INTAKE_SYSTEM_PROMPT = """You normalize user requests into a strict JSON TaskIntent object.

Return exactly one JSON object and nothing else.
Do not wrap the JSON in markdown (no ```json or ``` code blocks).

REQUIRED KEYS IN OUTPUT JSON:
- "goal": (string, REQUIRED) Concise description of the request.
- "project_type": (string) "python", "bash", "analysis", or "general".
- "capabilities": (list) e.g., ["scaffold"], ["bash", "llm_synthesis"].
- "constraints": (object) Default {}.
- "missing_inputs": (list) Default [].

FEW-SHOT EXAMPLES:
User Request: "Summarize /home/seeker/projects/FreeTalon"
JSON Output:
{
  "goal": "Summarize directory structure and contents of /home/seeker/projects/FreeTalon",
  "project_type": "analysis",
  "capabilities": ["bash", "llm_synthesis"],
  "constraints": {},
  "missing_inputs": []
}
"""

PLANNER_SYSTEM_PROMPT = """You convert a TaskIntent JSON object into an execution DAG JSON object.

Produce a dependency-safe directed acyclic graph in the "nodes" array. Do not create cycles: every node's "depends_on" must list only earlier nodes.

Return EXACTLY one valid JSON object and nothing else.
Do NOT wrap output in Markdown code blocks (no ```json or ```).

CRITICAL CAPABILITY & SCHEMA RULES:
1. "acceptance", "outputs", and "depends_on" MUST ALWAYS be JSON arrays of strings (e.g. ["Command exits with code 0"]), NEVER a bare string.
2. "scaffold": Writes files to disk (inputs MUST have "file_path" and "content").
3. "bash": Runs shell commands (inputs MUST have "command").
4. "llm_synthesis": Summarizes or analyzes data from previous steps. 
   - Inputs MUST use context piping: {"instruction": "...", "context": "{{step_1.stdout}}"}
5. Root nodes MUST have "depends_on": []. Downstream nodes MUST list parent step IDs in "depends_on".
6. "error" MUST be null.

FEW-SHOT EXAMPLES:

[Example 1: File Creation]
Input Goal: "Write a python script at /tmp/app.py that prints hi"
Output DAG:
{
  "nodes": [
    {
      "id": "step_1",
      "objective": "Scaffold python script at /tmp/app.py",
      "depends_on": [],
      "assigned_claw": "scaffold",
      "status": "draft",
      "inputs": {"file_path": "/tmp/app.py", "content": "print('hi')\\n"},
      "outputs": ["/tmp/app.py"],
      "acceptance": ["File exists"],
      "error": null
    }
  ],
  "metadata": {"estimated_steps": 1}
}

[Example 2: Read & Summarize Pipeline]
Input Goal: "Summarize directory structure and contents of /home/projects/FreeTalon"
Output DAG:
{
  "nodes": [
    {
      "id": "step_1",
      "objective": "Inspect directory contents",
      "depends_on": [],
      "assigned_claw": "bash",
      "status": "draft",
      "inputs": {"command": "ls -la /home/projects/FreeTalon"},
      "outputs": ["directory_listing"],
      "acceptance": ["Command exits with code 0"],
      "error": null
    },
    {
      "id": "step_2",
      "objective": "Synthesize and summarize directory listing",
      "depends_on": ["step_1"],
      "assigned_claw": "llm_synthesis",
      "status": "draft",
      "inputs": {
        "instruction": "Provide a high-level summary of the files and structure in this directory.",
        "context": "{{step_1.stdout}}"
      },
      "outputs": ["final_summary"],
      "acceptance": ["Summary generated successfully"],
      "error": null
    }
  ],
  "metadata": {"estimated_steps": 2}
}
"""
