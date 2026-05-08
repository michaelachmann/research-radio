"""
Extraction Analyzer - Structured information extraction from academic papers.

Extracts methodology, key findings, limitations, open questions, and more
into a machine-readable JSON file suitable for literature management and
weekly research runner summaries.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from .base import PaperAnalyzer, AnalysisResult
from src.claude_client import ClaudeClient
from config import ANALYSES_DIR


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "one_sentence_summary": {
            "type": "string",
            "description": "A single sentence capturing the core contribution of the paper.",
        },
        "research_context": {
            "type": "string",
            "description": "Background and domain context: what field, what problem, why it matters.",
        },
        "methodology": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "computational", "experimental", "survey",
                        "qualitative", "mixed", "theoretical", "review",
                    ],
                },
                "description": {
                    "type": "string",
                    "description": "Concise description of the research method and approach.",
                },
                "datasets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Named datasets or data sources used.",
                },
                "tools_and_methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key tools, algorithms, models, or statistical methods.",
                },
            },
            "required": ["type", "description"],
        },
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Main empirical or theoretical findings (3–6 items).",
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Acknowledged or apparent limitations of the study.",
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Unresolved questions or gaps highlighted by this work.",
        },
        "future_work": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Explicitly mentioned or implied directions for future research.",
        },
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5–10 keywords or concepts central to this paper.",
        },
    },
    "required": [
        "one_sentence_summary",
        "research_context",
        "methodology",
        "key_findings",
        "limitations",
        "open_questions",
    ],
}

SYSTEM_PROMPT = (
    "You are a research analyst specializing in computational social science, "
    "platform studies, and misinformation research. Extract structured information "
    "from academic papers accurately and concisely. Focus on what is explicitly "
    "stated in the paper; do not speculate."
)


class ExtractionAnalyzer(PaperAnalyzer):
    """Extracts structured metadata from a paper into a JSON file."""

    name = "extraction"

    def __init__(self):
        self.claude = ClaudeClient()

    def analyze(self, paper, paper_text: str) -> AnalysisResult:
        print(f"  [extraction] Extracting structured data...")

        prompt = f"""Extract structured information from the following academic paper.

Paper Title: {paper.title}
Authors: {', '.join(paper.authors) if paper.authors else 'Unknown'}

Paper Content:
{paper_text[:80000]}

Use the extract tool to return all fields. Be precise and base your answers
strictly on what the paper states."""

        data = self.claude.generate_json(
            prompt=prompt,
            schema=EXTRACTION_SCHEMA,
            tool_name="extract",
            system=SYSTEM_PROMPT,
        )

        if not data:
            return AnalysisResult(
                analyzer_name=self.name,
                paper_id=paper.id,
                success=False,
                error="Claude returned no structured data",
            )

        # Add envelope metadata
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

        print(f"  [extraction] Saved: {output_path}")
        return AnalysisResult(
            analyzer_name=self.name,
            paper_id=paper.id,
            success=True,
            artifacts=[output_path],
            data=output,
        )

    def _get_output_path(self, paper_id: str) -> str:
        safe_id = paper_id.replace("bibtex:", "").replace("/", "_").replace("\\", "_")
        return os.path.join(ANALYSES_DIR, f"{safe_id[:100]}_extraction.json")
