"""
Analyzer registry — maps string names to PaperAnalyzer subclasses.

To add a new analyzer:
1. Create a module in src/analyzers/ with a PaperAnalyzer subclass
2. Import and register it in REGISTRY below
3. Add its name to ENABLED_ANALYZERS in .env or the GitHub Actions workflow
"""

from .base import PaperAnalyzer, AnalysisResult
from .podcast import PodcastAnalyzer
from .extraction import ExtractionAnalyzer
from .critical import CriticalAnalyzer

REGISTRY: dict[str, type[PaperAnalyzer]] = {
    "podcast": PodcastAnalyzer,
    "extraction": ExtractionAnalyzer,
    "critical": CriticalAnalyzer,
}


def load_analyzers(names: list[str]) -> list[PaperAnalyzer]:
    """Instantiate and return analyzers for the given names (in order)."""
    analyzers = []
    for name in names:
        if name in REGISTRY:
            analyzers.append(REGISTRY[name]())
        else:
            print(f"Warning: unknown analyzer '{name}' — skipping")
    return analyzers


__all__ = [
    "PaperAnalyzer",
    "AnalysisResult",
    "REGISTRY",
    "load_analyzers",
]
