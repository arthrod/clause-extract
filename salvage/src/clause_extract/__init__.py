"""clause-extract: parser for SEC EX-10 exhibit agreements.

Extracts clause content with hierarchical relationships and document-level
nuances (subdocuments, Reading B exclusions, depth bounds, parser-disagreement
audit signals). Output is a structured per-document dataset; downstream
consumers build categorization, retrieval, or statistics on top of it.

See README.md for goals and architecture.
"""

__version__ = "0.1.0"
