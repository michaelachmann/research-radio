"""
Base classes for the pluggable analyzer framework.

To add a new analyzer:
1. Create a new module in src/analyzers/
2. Subclass PaperAnalyzer, set name, implement analyze()
3. Register it in src/analyzers/__init__.py REGISTRY
4. Add the name to ENABLED_ANALYZERS in .env or the workflow
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AnalysisResult:
    """Result returned by any PaperAnalyzer.analyze() call."""
    analyzer_name: str
    paper_id: str
    success: bool
    artifacts: list[str] = field(default_factory=list)  # absolute paths to files created
    data: Optional[dict] = None   # structured output (JSON-serializable)
    error: Optional[str] = None


class PaperAnalyzer(ABC):
    """Abstract base class for all paper analyzers."""

    name: str  # must be set on each subclass; used as key in REGISTRY

    @abstractmethod
    def analyze(self, paper, paper_text: str) -> AnalysisResult:
        """
        Analyze a paper and return a result.

        Args:
            paper: Paper dataclass from feed_parser
            paper_text: Full extracted text of the paper

        Returns:
            AnalysisResult with success flag, any artifacts created, and optional data dict
        """
        ...
