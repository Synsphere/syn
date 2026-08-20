from .cog import router as cogRouter
from .credential import router as credentialRouter
from .ember import router as emberRouter
from .enigma import router as enigmaRouter
from .event import router as eventRouter
from .om import router as omRouter
from .pondle import router as pondleRouter
from .snug import router as snugRouter
from .thing import router as thingRouter
from .type import router as typeRouter


primitiveRouters = (
    cogRouter,
    credentialRouter,
    emberRouter,
    enigmaRouter,
    eventRouter,
    omRouter,
    pondleRouter,
    snugRouter,
    thingRouter,
    typeRouter,
)

__all__ = [
    "cogRouter",
    "credentialRouter",
    "emberRouter",
    "enigmaRouter",
    "eventRouter",
    "omRouter",
    "pondleRouter",
    "primitiveRouters",
    "snugRouter",
    "thingRouter",
    "typeRouter",
]
