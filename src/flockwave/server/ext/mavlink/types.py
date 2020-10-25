"""Types commonly used throughout the MAVLink module."""

from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Dict, Optional, Tuple, Union

__all__ = (
    "MAVLinkMessage",
    "MAVLinkMessageSpecification",
    "MAVLinkNetworkSpecification",
    "spec",
)


#: Type specification for messages parsed by the MAVLink parser. Unfortunately
#: we cannot refer to an exact Python class here because that depends on the
#: dialoect that we will be parsing
MAVLinkMessage = Any

#: Type specification for MAVLink message matchers. A message matcher is either
#: `None` (meaning to match all messages), a dictionary containing the required
#: field name-value pairs in a message that we need to consider the message to
#: be a match, or a callable that takes a MAVLinkMessage and returns `True` if
#: the message is a match
MAVLinkMessageMatcher = Optional[
    Union[Dict[str, Any], Callable[[MAVLinkMessage], bool]]
]

#:
MAVLinkMessageSpecification = Tuple[str, Dict[str, Any]]


def _spec(name, **kwds):
    return (name, kwds)


class _MAVLinkMessageSpecificationFactory:
    """Convenience constructor for MAVLink message specifications."""

    def __init__(self):
        self._cache = {}

    def __getattr__(self, name):
        name = name.upper()
        func = self._cache.get(name)
        if not func:
            self._cache[name] = func = partial(self._match, name)
        return func

    @staticmethod
    def _match(name, *args, **kwds):
        if args:
            if kwds:
                raise RuntimeError(
                    "mixing function matchers with keyword arguments not supported"
                )
            if len(args) > 1:
                raise RuntimeError("only one matcher function is supported")
            return (name, args[0])
        else:
            return (name, kwds)


spec = _MAVLinkMessageSpecificationFactory()


@dataclass
class MAVLinkNetworkSpecification:
    """Parameter specification for a single MAVLink network."""

    #: Unique identifier of this MAVLink network that the server can use as a
    #: priamry key
    id: str

    #: MAVLink system ID reserved for the ground station (the Skybrush server)
    system_id: int = 255

    #: Python format string that receives the MAVLink system ID of a drone
    #: and the network ID, and returns its preferred formatted identifier that
    #: is used when the drone is registered in the server
    id_format: str = "{1}{0}"

    #: The connections that the MAVLink network will consist of. A MAVLink
    #: network may have one or more connections where MAVLink messages are
    #: received and sent, but a system ID appearing on one of the networks
    #: identifies the _same_ device as the same system ID on another network.
    #: In most cases, one link is the primary one and the rest are used as
    #: "backups".
    #:
    #: The dictionary may contain any values that are accepted by th
    #: `create_connection()` function.
    connections: Dict[str, Any] = field(default_factory=dict)

    #: Whether to simulate packet loss in this network by randomly dropping
    #: received and sent messages. Zero means the normal behaviour, otherwise
    #: it is interpreted as the probability of a lost MAVLink message.
    packet_loss: float = 0

    @classmethod
    def from_json(cls, obj, id: Optional[str] = None) -> "MAVLinkNetworkSpecification":
        """Constructs a MAVLink network specification from its JSON
        representation.
        """
        result = cls(id=id if id is not None else obj["id"])

        if "system_id" in obj:
            result.system_id = obj["system_id"]

        if "id_format" in obj:
            result.id_format = obj["id_format"]

        if "connections" in obj:
            result.connections = obj["connections"]

        if "packet_loss" in obj:
            result.packet_loss = float(obj["packet_loss"])

        return result

    @property
    def json(self):
        """Returns the JSON representation of the network specification."""
        return {
            "id": self.id,
            "id_format": self.id_format,
            "system_id": self.system_id,
            "connections": self.connections,
            "packet_loss": self.packet_loss,
        }
