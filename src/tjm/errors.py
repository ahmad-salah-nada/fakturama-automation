"""Expected extraction failures become reviewable reports, not guessed data."""


class ExtractionError(Exception):
    def __init__(self, message, *, code="unreadable", field=None, evidence=None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.evidence = evidence or {}
