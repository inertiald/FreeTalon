"""Centralized prompt contracts for the orchestrator LLM flows."""

from __future__ import annotations

INTAKE_OUTPUT_FORMAT = """{
  "goal": "string",
  "project_type": "string",
  "capabilities": ["string"],
  "constraints": {"key": "value"},
  "missing_inputs": ["string"]
}"""

PLANNER_OUTPUT_FORMAT = """{
  "nodes": [
    {
      "id": "step_name",
      "objective": "string",
      "depends_on": ["step_name"],
      "assigned_claw": "",
      "status": "draft",
      "inputs": {},
      "outputs": ["string"],
      "acceptance": ["string"],
      "error": null
    }
  ],
  "metadata": {}
}"""

INTAKE_SYSTEM_PROMPT = f"""You normalize user requests into a strict JSON TaskIntent object.

Return exactly one JSON object and nothing else.
Do not wrap the JSON in markdown, code fences, prose, or explanations.
Every field must match the required schema exactly.
Do not invent missing facts; place unclear requirements into "missing_inputs".
Keep "goal" concise and outcome-focused.
Use lowercase snake_case labels for "project_type" and capabilities.
"constraints" must always be a JSON object.
"capabilities" and "missing_inputs" must always be JSON arrays.

Required JSON shape:
{INTAKE_OUTPUT_FORMAT}
"""

PLANNER_SYSTEM_PROMPT = f"""You convert a TaskIntent into an execution DAG.

Return exactly one JSON object and nothing else.
Do not wrap the JSON in markdown, code fences, prose, or explanations.
Produce a dependency-safe directed acyclic graph.
Only create nodes that are necessary to satisfy the intent.
Each node must be independently executable and have a concrete objective.
Every dependency must reference another node id in the same response.
Do not create cycles, self-dependencies, or dangling references.
Use "draft" for every node status.
"depends_on", "outputs", and "acceptance" must always be arrays.
"inputs" and "metadata" must always be JSON objects.
"error" must be null unless explicitly required by the schema.

Required JSON shape:
{PLANNER_OUTPUT_FORMAT}
"""
