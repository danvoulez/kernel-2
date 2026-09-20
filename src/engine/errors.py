class EngineError(Exception):
    """Base class for errors safe to expose at the API boundary."""


class ValidationError(EngineError):
    pass


class NotFound(EngineError):
    pass


class Conflict(EngineError):
    pass


class IntegrityError(EngineError):
    pass

