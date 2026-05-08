"""
Critical Analyzer - Evaluates strengths, weaknesses, and research quality.

Produces a structured critical review of the paper suitable for literature
curation and weekly research runner summaries.
"""

import json
import os
from datetime import datetime, timezone

from .base import PaperAnalyzer, AnalysisResult
from src.claude_client import ClaudeClient
from config import ANALYSES_DIR


CRITICAL_SCHEMA = {
    "type": "object",
    "properties": {
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key strengths of the paper (methodology, novelty, clarity, etc.).",
        },
        "weaknesses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key weaknesses or concerns (methodological, scope, generalizability, etc.).",
        },
        "reproducibility": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": (
                "High: code/data available, methods fully described. "
                "Medium: methods described but artifacts unavailable. "
                "Low: insufficient detail to reproduce."
            ),
        },
        "novelty": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "How novel is the contribution relative to prior work?",
        },
        "significance": {
            "type": "string",
            "description": "Why does this paper matter? What is its broader impact?",
        },
        "recommended_for": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Audiences or research communities who should read this (e.g., 'misinformation researchers', 'NLP practitioners').",
        },
        "overall_assessment": {
            "type": "string",
            "description": "2–4 sentence balanced assessment of the paper's contribution and limitations.",
        },
    },
    "required": [
        "strengths",
        "weaknesses",
        "reproducibility",
        "novelty",
        "overall_assessment",
    ],
}

SYSTEM_PROMPT = (
    "You are a critical reviewer for a top-tier academic venue in computational "
    "social science and AI. Provide balanced, evidence-based assessments. "
    "Be constructive — acknowledge genuine strengths while identifying real weaknesses."
)


class CriticalAnalyzer(PaperAnalyzer):
    """Produces a structured critical review of a paper."""

    name = "critical"

    def __init__(self):
        self.claude = ClaudeClient()

    def analyze(self, paper, paper_text: str) -> AnalysisResult:
        print(f"  [critical] Generating critical review...")

        prompt = f"""Critically review the following academic paper.

Paper Title: {paper.title}
Authors: {', '.join(paper.authors) if paper.authors else 'Unknown'}

Paper Content:
{paper_text[:80000]}

Provide a balanced critical assessment covering strengths, weaknesses,
reproducibility, novelty, significance, and an overall assessment.
Base your evaluation strictly on the paper's content."""

        data = self.claude.generate_json(
            prompt=prompt,
            schema=CRITICAL_SCHEMA,
            tool_name="review",
            system=SYSTEM_PROMPT,
        )

        if not data:
            return AnalysisResult(
                analyzer_name=self.name,
                paper_id=paper.id,
                success=False,
                error="Claude returned no structured data",
            )

        output = {
            "paper_id": paper.id,
            "paper_title": paper.title,
            "paper_authors": paper.authors,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "analyzer": self.name,
            **data,
        }

        output_path = self._get_output_path(paper.id)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        print(f"  [critical] Saved: {output_path}")
        return AnalysisResult(
            analyzer_name=self.name,
            paper_id=paper.id,
            success=True,
            artifacts=[output_path],
            data=output,
        )

    def _get_output_path(self, paper_id: str) -> str:
        safe_id = paper_id.replace("bibtex:", "").replace("/", "_").replace("\\", "_")
        return os.path.join(ANALYSES_DIR, f"{safe_id[:100]}_critical.json")
