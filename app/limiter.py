"""The single slowapi limiter instance, shared by every router.

Kept in its own module so both ``app.main`` and ``app.agentic`` decorate their
routes against the SAME limiter (and ``app.state.limiter`` is that instance),
without a circular import between the two route modules. The test suite resets
this one instance between tests via ``app.main.limiter.reset()``.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
