"""Classes related to message handling in Flockwave."""

from __future__ import absolute_import

import attr

from abc import ABCMeta, abstractmethod
from collections import defaultdict
from contextlib import contextmanager
from functools import partial
from inspect import isawaitable
from itertools import chain
from jsonschema import ValidationError
from trio import (
    ClosedResourceError,
    Event,
    move_on_after,
    open_memory_channel,
    open_nursery,
    sleep,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generator,
    Iterable,
    Optional,
    Tuple,
    Union,
)

from flockwave.connections import ConnectionState

from .concurrency import AsyncBundler
from .logger import log as base_log
from .model import (
    Client,
    FlockwaveMessage,
    FlockwaveMessageBuilder,
    FlockwaveNotification,
    FlockwaveResponse,
)
from .registries import ClientRegistry

__all__ = (
    "ConnectionStatusMessageRateLimiter",
    "GenericRateLimiter",
    "MessageHub",
    "RateLimiters",
)

log = base_log.getChild("message_hub")


MessageHandler = Callable[[FlockwaveMessage, Client, "MessageHub"], FlockwaveMessage]


class MessageValidationError(RuntimeError):
    """Error that is thrown by the MessageHub_ class internally when it
    fails to validate an incoming message against the Flockwave schema.
    """

    pass


@attr.s
class Request:
    """Single message sending request in the queue of the message hub."""

    #: The message to send
    message: FlockwaveMessage = attr.ib()

    #: The client that will receive the message; `None` means that it is a
    #: broadcast
    to: Optional[Union[str, Client]] = attr.ib(default=None)

    #: Another, optional message that this message responds to
    in_response_to: Optional[FlockwaveMessage] = attr.ib(default=None)


class MessageHub:
    """Central entity in a Flockwave server that handles incoming messages
    and prepares outbound messages for dispatch.

    Users of this class can register message handlers that will be invoked
    for incoming messages based on the type of the incoming message using
    the `register_message_handler()`_ method. The method allows one to
    register specific handlers for one or more message types as well as
    generic message handlers that are invoked for all messages. Specific
    handlers are always invoked before generic ones; otherwise the handlers
    are called in the order they were registered.

    Message handler functions are required to return one of the following
    values:

    - ``False`` or ``None`` if they decided not to handle the message they
      were given.

    - ``True`` to indicate that they have handled the message and no further
      action is needed

    - A ``dict`` that contains a response body that should be sent back to
      the caller. In this case, the hub will wrap the body in an appropriate
      message envelope that refers to the ID of the message that the
      object responds to.

    - A FlockwaveResponse_ object that is *already* constructed in a way
      that it responds to the original message.

    When the body of the response returned from the handler does not contain
    a message type (i.e. a ``type`` key), it will be added automatically,
    assuming that it is equal to the type of the incoming message.
    """

    def __init__(self):
        """Constructor."""
        self._handlers_by_type = defaultdict(list)
        self._message_builder = FlockwaveMessageBuilder()
        self._broadcast_methods = None
        self._channel_type_registry = None
        self._client_registry = None

        self._queue_tx, self._queue_rx = open_memory_channel(4096)

    def acknowledge(self, message=None, outcome=True, reason=None):
        """Creates a new positive or negative acknowledgment of the given
        message.

        Parameters:
            message (FlockwaveMessage): the message to respond to with a
                positive or negative acknowledgmnt
            outcome (bool): ``True`` to construct a positive acknowledgment,
                ``False`` otherwise.
            reason (Optional[str]): the reason for a negative acknowledgment;
                ignored for positive acknowledgments

        Returns:
            FlockwaveResponse: a response object that acknowledges the
                given message
        """
        body = {"type": "ACK-ACK" if outcome else "ACK-NAK"}
        if not outcome and reason:
            body["reason"] = reason
        return self._message_builder.create_response_to(message, body)

    async def broadcast_message(self, message):
        """Sends a broadcast message from this message hub.

        Blocks until the message was actually broadcast to all connected
        clients. If you are not interested in when the message is
        sent, use `enqueue_broadcast_message()` instead.

        Parameters:
            message (FlockwaveNotification): the notification to broadcast.
        """
        assert isinstance(
            message, FlockwaveNotification
        ), "only notifications may be broadcast"
        await self._queue_tx.send(Request(message))

    @property
    def channel_type_registry(self):
        """Registry that keeps track of the different channel types that the
        app can handle. This is used by the message hub to figure out how to
        broadcast messages to all connected clients.
        """
        return self._channel_type_registry

    @channel_type_registry.setter
    def channel_type_registry(self, value):
        if self._channel_type_registry == value:
            return

        if self._channel_type_registry is not None:
            self._channel_type_registry.added.disconnect(
                self._invalidate_broadcast_methods, sender=self._channel_type_registry
            )
            self._channel_type_registry.removed.disconnect(
                self._invalidate_broadcast_methods, sender=self._channel_type_registry
            )

        self._channel_type_registry = value

        if self._channel_type_registry is not None:
            self._channel_type_registry.added.connect(
                self._invalidate_broadcast_methods, sender=self._channel_type_registry
            )
            self._channel_type_registry.removed.connect(
                self._invalidate_broadcast_methods, sender=self._channel_type_registry
            )

    @property
    def client_registry(self):
        """Registry that keeps track of connected clients so the message hub
        can broadcast messages to all connected clients.
        """
        return self._client_registry

    @client_registry.setter
    def client_registry(self, value):
        if self._client_registry == value:
            return

        if self._client_registry is not None:
            self._client_registry.added.disconnect(
                self._invalidate_broadcast_methods, sender=self._client_registry
            )
            self._client_registry.removed.disconnect(
                self._invalidate_broadcast_methods, sender=self._client_registry
            )

        self._client_registry = value

        if self._client_registry is not None:
            self._client_registry.added.connect(
                self._invalidate_broadcast_methods, sender=self._client_registry
            )
            self._client_registry.removed.connect(
                self._invalidate_broadcast_methods, sender=self._client_registry
            )

    def create_notification(self, body=None):
        """Creates a new Flockwave notification to be sent by the server.

        Parameters:
            body (object): the body of the notification.

        Returns:
            FlockwaveNotification: a notification object
        """
        return self._message_builder.create_notification(body)

    def create_response_or_notification(self, body=None, in_response_to=None):
        """Creates a new Flockwave response or notification object,
        depending on whether the caller specifies a message to respond to
        or not.

        Parameters:
            body (object): the body of the response.
            message (FlockwaveMessage or None): the message to respond to
                or ``None`` if we want to createa a notification instead.

        Returns:
            FlockwaveMessage: a response or notification object
        """
        if in_response_to is None:
            return self.create_notification(body)
        else:
            return self.create_response_to(in_response_to, body)

    def create_response_to(self, message, body=None):
        """Creates a new Flockwave response object that will respond to the
        given message.

        Parameters:
            message (FlockwaveMessage): the message to respond to
            body (object): the body of the response.

        Returns:
            FlockwaveResponse: a response object that will respond to the
                given message
        """
        return self._message_builder.create_response_to(message, body)

    def enqueue_broadcast_message(self, message):
        """Sends a broadcast message from this message hub.

        Blocks until the message was actually broadcast to all connected
        clients. If you are not interested in when the message is
        sent, use `enqueue_broadcast_message()` instead.

        Parameters:
            message (FlockwaveNotification): the notification to broadcast.
        """
        assert isinstance(
            message, FlockwaveNotification
        ), "only notifications may be broadcast"
        self._queue_tx.send_nowait(Request(message))

    def enqueue_message(
        self,
        message: Union[FlockwaveMessage, Dict[str, Any]],
        to: Optional[Union[str, Client]] = None,
        in_response_to: Optional[FlockwaveMessage] = None,
    ) -> None:
        """Enqueues a message or notification in this message hub to be
        sent later.

        Notifications are sent to all connected clients, unless ``to`` is
        specified, in which case they are sent only to the given client.

        Messages are sent only to the client whose request is currently
        being served, unless ``to`` is specified, in which case they are
        sent only to the given client.

        Note that this function may drop messages if they are enqueued too
        fast. Use `send_message()` if you want to block until the message
        is actually in the queue.

        Parameters:
            message: the message to enqueue, or its body (in which case
                an appropriate envelope will be added automatically)
            to: the Client_ object that represents the recipient of the message,
                or the ID of the client. ``None`` means to send the message to
                all connected clients.
            in_response_to: the message that the message being sent responds to.
        """
        if not isinstance(message, FlockwaveMessage):
            message = self.create_response_or_notification(
                message, in_response_to=in_response_to
            )
        if to is None:
            assert in_response_to is None, (
                "broadcast messages cannot be sent in response to a "
                "particular message"
            )
            return self.enqueue_broadcast_message(message)
        else:
            self._queue_tx.send_nowait(
                Request(message, to=to, in_response_to=in_response_to)
            )

    async def handle_incoming_message(self, message, sender):
        """Handles an incoming Flockwave message by calling the appropriate
        message handlers.

        Parameters:
            message (dict): the incoming message, already decoded from
                its string representation into a Python dict, but before
                it was validated against the Flockwave schema
            sender (Client): the sender of the message

        Returns:
            bool: whether the message was handled by at least one handler
                or internally by the hub itself
        """
        try:
            message = self._decode_incoming_message(message)
        except MessageValidationError as ex:
            reason = str(ex)
            log.exception(reason)
            if "id" in message:
                ack = self.acknowledge(message, outcome=False, reason=reason)
                await self.send_message(ack, to=sender)
                return True
            else:
                return False

        if "error" in message:
            log.warning("Error message from Flockwave client silently dropped")
            return True

        log.info(
            "Received {0.body[type]} message".format(message),
            extra={"id": message.id, "semantics": "request"},
        )

        handled = await self._feed_message_to_handlers(message, sender)

        if not handled:
            log.warning(
                "Unhandled message: {0.body[type]}".format(message),
                extra={"id": message.id},
            )
            ack = self.acknowledge(
                message,
                outcome=False,
                reason="No handler managed to parse this message in the server",
            )
            await self.send_message(ack, to=sender)
            return False

        return True

    async def iterate(
        self, *args
    ) -> Generator[Tuple[Dict[str, Any], Client, Callable[[Dict], None]], None, None]:
        """Returns an async generator that yields triplets consisting of
        the body of an incoming message, its sender and an appropriate function
        that can be used to respond to that message.

        The generator yields triplets containing only those messages whose type
        matches the message types provided as arguments.

        Messages can be responded to by calling the responder function with
        the response itself. The responder function returns immediately after
        queueing the message for dispatch; it does not wait for the message
        to actually be dispatched.

        It is assumed that all messages are handled by the consumer of the
        generator; it is not possible to leave messages unhandled. Not sending
        a response to a message is okay as long as it is allowed by the
        communication protocol.

        Yields:
            the body of an incoming message, its sender and the responder
            function
        """
        to_handlers, from_clients = open_memory_channel(0)

        async def handle(message, sender, hub):
            await to_handlers.send((message, sender))
            return True

        with self.use_message_handler(handle, args):
            while True:
                message, sender = await from_clients.receive()
                if message.body:
                    responder = partial(
                        self.enqueue_message, to=sender, in_response_to=message
                    )
                    yield message.body, sender, responder

    def _commit_broadcast_methods(self):
        """Calculates the list of methods to call when the message hub
        wishes to broadcast a message to all the connected clients.
        """
        assert isinstance(
            self.client_registry, ClientRegistry
        ), "message hub does not have a client registry yet"

        result = []
        clients_for = self._client_registry.client_ids_for_channel_type
        has_clients_for = self._client_registry.has_clients_for_channel_type

        for channel_type_id in self._channel_type_registry.ids:
            descriptor = self._channel_type_registry[channel_type_id]
            broadcaster = descriptor.broadcaster
            if broadcaster:
                if has_clients_for(descriptor.id):
                    result.append(broadcaster)
            else:
                clients = clients_for(descriptor.id)
                for client_id in clients:
                    result.append(partial(self._send_message, to=client_id))

        return result

    async def _feed_message_to_handlers(self, message, sender):
        """Forwards an incoming, validated Flockwave message to the message
        handlers registered in the message hub.

        Parameters:
            message (FlockwaveMessage): the message to process
            sender (Client): the sender of the message

        Returns:
            bool: whether the message was handled by at least one handler
        """
        # TODO(ntamas): right now the handlers are executed in sequence so
        # one handler could arbitrarily delay the ones coming later in the
        # queue

        message_type = message.body["type"]
        all_handlers = chain(
            self._handlers_by_type.get(message_type, ()), self._handlers_by_type[None]
        )

        handled = False
        for handler in all_handlers:
            try:
                response = handler(message, sender, self)
            except Exception:
                log.exception(
                    "Error while calling handler {0!r} "
                    "for incoming message; proceeding with "
                    "next handler (if any)".format(handler)
                )
                response = None

            if isawaitable(response):
                try:
                    response = await response
                except Exception:
                    log.exception(
                        "Error while waiting for response from handler "
                        "{0!r} for incoming message; proceeding with "
                        "next handler (if any)".format(handler)
                    )
                    response = None

            if response is True:
                # Message was handled by the handler
                handled = True
            elif response is False or response is None:
                # Message was rejected by the handler, nothing to do
                pass
            elif isinstance(response, (dict, FlockwaveResponse)):
                # Handler returned a dict or a response; we must send it
                await self._send_response(response, to=sender, in_response_to=message)
                handled = True

        return handled

    def _invalidate_broadcast_methods(self, *args, **kwds):
        """Invalidates the list of methods to call when the message hub
        wishes to broadcast a message to all the connected clients.
        """
        self._broadcast_methods = None

    def on(self, *args):
        """Decorator factory function that allows one to register a message
        handler on a MessageHub_ with the following syntax::

            @message_hub.on("SYS-VER")
            def handle_SYS_VER(message, sender, hub):
                [...]
        """

        def decorator(func):
            self.register_message_handler(func, args)
            return func

        return decorator

    def register_message_handler(
        self, func: MessageHandler, message_types: Optional[Iterable[str]] = None
    ) -> None:
        """Registers a handler function that will handle incoming messages.

        It is possible to register the same handler function multiple times,
        even for the same message type.

        Parameters:
            func: the handler to register. It will be called with the incoming
                message and the message hub object.

            message_types: an iterable that yields the message types for which
                this handler will be registered. ``None`` means to register the
                handler for all message types. The handler function must return
                ``True`` if it has handled the message successfully, ``False``
                if it skipped the message. Note that returning ``True`` will not
                prevent other handlers from getting the message.
        """
        if message_types is None or isinstance(message_types, str):
            message_types = [message_types]

        for message_type in message_types:
            if message_type is not None and not isinstance(message_type, str):
                message_type = message_type.decode("utf-8")
            self._handlers_by_type[message_type].append(func)

    async def run(self, seconds=1):
        """Runs the message hub periodically in an infinite loop. This
        method should be launched in a Trio nursery.
        """
        async with open_nursery() as nursery, self._queue_rx:
            async for request in self._queue_rx:
                if request.to:
                    nursery.start_soon(
                        self._send_message,
                        request.message,
                        request.to,
                        request.in_response_to,
                    )
                else:
                    nursery.start_soon(self._broadcast_message, request.message)

    async def send_message(
        self,
        message: Union[FlockwaveMessage, Dict[str, Any]],
        to: Optional[Union[str, Client]] = None,
        in_response_to: Optional[FlockwaveMessage] = None,
    ) -> None:
        """Sends a message or notification from this message hub.

        Notifications are sent to all connected clients, unless ``to`` is
        specified, in which case they are sent only to the given client.

        Messages are sent only to the client whose request is currently
        being served, unless ``to`` is specified, in which case they are
        sent only to the given client.

        Parameters:
            message (FlockwaveMessage): the message to send.
            to (Optional[Union[str, Client]]): the Client_ object that
                represents the recipient of the message, or the ID of the
                client. ``None`` means to send the message to all connected
                clients.
            in_response_to (Optional[FlockwaveMessage]): the message that
                the message being sent responds to.
        """
        if not isinstance(message, FlockwaveMessage):
            message = self.create_response_or_notification(
                message, in_response_to=in_response_to
            )
        if to is None:
            assert in_response_to is None, (
                "broadcast messages cannot be sent in response to a "
                "particular message"
            )
            return await self.broadcast_message(message)
        await self._queue_tx.send(
            Request(message, to=to, in_response_to=in_response_to)
        )

    def unregister_message_handler(
        self, func: MessageHandler, message_types: Optional[Iterable[str]] = None
    ) -> None:
        """Unregisters a handler function from the given message types.

        Parameters:
            func: the handler to unregister.
            message_types: an iterable that yields the message types from which
                this handler will be unregistered. ``None`` means to unregister
                the handler from all message types; however, if it was also
                registered for specific message types individually (in addition
                to all messages in general), it will also have to be unregistered
                from the individual message types.
        """
        if message_types is None or isinstance(message_types, str):
            message_types = [message_types]

        for message_type in message_types:
            if message_type is not None and not isinstance(message_type, str):
                message_type = message_type.decode("utf-8")
            handlers = self._handlers_by_type.get(message_type)
            try:
                handlers.remove(func)
            except ValueError:
                # Handler not in list; no problem
                pass

    @contextmanager
    def use_message_handler(
        self, func: MessageHandler, message_types: Optional[Iterable[str]] = None
    ) -> None:
        """Context manager that registers a handler function that will handle
        incoming messages, and unregisters the function upon exiting the
        context.

        Parameters:
            func: the handler to register. It will be called with the incoming
                message, the sender and the message hub object.

            message_types: an iterable that yields the message types for which
                this handler will be registered. ``None`` means to register the
                handler for all message types. The handler function must return
                ``True`` if it has handled the message successfully, ``False``
                if it skipped the message. Note that returning ``True`` will not
                prevent other handlers from getting the message.
        """
        try:
            self.register_message_handler(func, message_types)
            yield
        finally:
            self.unregister_message_handler(func, message_types)

    def _decode_incoming_message(self, message):
        """Decodes an incoming, raw JSON message that has already been
        decoded from the string representation into a dictionary on the
        Python side, but that has not been validated against the Flockwave
        message schema.

        Parameters:
            message (dict): the incoming, raw message

        Returns:
            message (FlockwaveMessage): the validated message as a Python
                FlockwaveMessage_ object

        Raises:
            MessageDecodingError: if the message could not have been decoded
        """
        try:
            return FlockwaveMessage.from_json(message)
        except ValidationError:
            # We should not re-raise directly from here because on Python 3.x
            # we would get a very long stack trace that includes the original
            # exception as well.
            error = MessageValidationError("Flockwave message does not match schema")
        except Exception as ex:
            # We should not re-raise directly from here because on Python 3.x
            # we would get a very long stack trace that includes the original
            # exception as well.
            error = MessageValidationError("Unexpected exception: {0!r}".format(ex))
        raise error

    def _log_message_sending(self, message, to=None, in_response_to=None):
        type = message.body.get("type") if hasattr(message, "body") else "NO-TYPE"

        if to is None:
            if type not in ("UAV-INF", "DEV-INF"):
                log.info(
                    f"Broadcasting {type} notification",
                    extra={"id": message.id, "semantics": "notification"},
                )
        elif in_response_to is not None:
            log.info(
                f"Sending {type} response",
                extra={"id": in_response_to.id, "semantics": "response_success"},
            )
        elif isinstance(message, FlockwaveNotification):
            if type not in ("UAV-INF", "DEV-INF"):
                log.info(
                    f"Sending {type} notification",
                    extra={"id": message.id, "semantics": "notification"},
                )
        else:
            print(repr(message))
            extra = {"semantics": "response_success"}
            if hasattr(message, "id"):
                extra["id"] = message.id

            log.info(f"Sending {type} message", extra=extra)

    async def _broadcast_message(self, message):
        if self._broadcast_methods is None:
            self._broadcast_methods = self._commit_broadcast_methods()

        if self._broadcast_methods:
            self._log_message_sending(message)
            failures = 0
            for func in self._broadcast_methods:
                try:
                    await func(message)
                except ClosedResourceError:
                    # client is probably gone; no problem
                    pass
                except Exception:
                    failures += 1

            if failures > 0:
                log.error(f"Error while broadcasting message to {failures} client(s)")

    async def _send_message(self, message, to, in_response_to=None):
        self._log_message_sending(message, to, in_response_to)
        if not isinstance(to, Client):
            try:
                client = self._client_registry[to]
            except KeyError:
                log.warn("Client is gone; not sending message", extra={"id": str(to)})
                return
        else:
            client = to

        try:
            await client.channel.send(message)
        except ClosedResourceError:
            log.warn("Client is gone; not sending message", extra={"id": client.id})
        except Exception:
            log.exception(
                "Error while sending message to client", extra={"id": client.id}
            )

    async def _send_response(self, message, to, in_response_to):
        """Sends a response to a message from this message hub.

        Parameters:
            message (FlockwaveResponse or object): the response, or the body
                of the response. When it is a FlockwaveResponse_ object, the
                function will check whether the response indeed refers to
                the given message (in the ``in_response_to`` parameter).
                When it is any other object, it will be wrapped in a
                FlockwaveResponse_ object first. In both cases, the type
                of the message body will be filled from the type of the
                original message if it is not given.
            to (Client):  a Client_ object that represents the intended
                recipient of the message.
            in_response_to (FlockwaveMessage): the message that the given
                object is responding to

        Returns:
            FlockwaveResponse: the response that was sent back to the client
        """
        if isinstance(message, FlockwaveResponse):
            assert message.correlationId == in_response_to.id
            response = message
        else:
            try:
                response = self._message_builder.create_response_to(
                    in_response_to, body=message
                )
            except Exception:
                log.exception("Failed to create response")
        await self.send_message(response, to=to, in_response_to=in_response_to)
        return response


class RateLimiter(metaclass=ABCMeta):
    """Abstract base class for rate limiter objects."""

    @abstractmethod
    def add_request(self, *args, **kwds) -> None:
        """Adds a new request to the rate limiter.

        The interpretation of positional and keyword arguments must be
        specialized and described in implementations of the RateLimiter_
        interface.
        """
        raise NotImplementedError

    @abstractmethod
    async def run(self, dispatcher) -> None:
        """Runs the task handling the messages emitted from this rate
        limiter.
        """
        raise NotImplementedError


@attr.s
class GenericRateLimiter(RateLimiter):
    """Generic rate limiter that is useful for most types of UAV-related
    messages that we want to rate-limit.

    The rate limiter receives requests containing lists of UAV IDs; these are
    the UAVs for which we want to send a specific type of message (say,
    UAV-INF). The first request is always executed immediately. For all
    subsequent requests, the rate limiter checks the amount of time elapsed
    since the last message dispatch. If it is greater than a prescribed delay,
    the message is sent immediately; otherwise the rate limiter waits and
    collects all UAV IDs for which a request arrives until the delay is reached,
    and _then_ sends a single message containing information about all UAVs
    that were referred recently.

    The rate limiter requires a factory function that takes a list of UAV IDs
    and produces a single FlockwaveMessage_ to send.
    """

    factory: Callable[[Iterable[str]], FlockwaveMessage] = attr.ib()
    name: Optional[str] = attr.ib(default=None)
    delay: float = attr.ib(default=0.1)

    bundler: AsyncBundler = AsyncBundler()

    def add_request(self, uav_ids: Iterable[str]) -> None:
        """Requests that the task handling the messages for this factory
        send the messages corresponding to the given UAV IDs as soon as
        the rate limiting rules allow it.
        """
        self.bundler.add_many(uav_ids)

    async def run(self, dispatcher, nursery):
        self.bundler.clear()
        async for bundle in self.bundler:
            try:
                await dispatcher(self.factory(bundle))
            except Exception:
                log.exception(
                    f"Error while dispatching messages from {self.name} factory"
                )
            await sleep(self.delay)


class ConnectionStatusMessageRateLimiter(RateLimiter):
    """Specialized rate limiter for CONN-INF (connection status) messages.

    For this rate limiter, the rules are as follows. Each request must convey
    a single connection ID and the corresponding new status of the connection
    that we wish to inform listeners about. When the new status is a stable
    status (i.e. "connected" or "disconnected"), a CONN-INF message is
    created and dispatched immediately. When the new status is a transitioning
    status (i.e. "connecting" or "disconnecting"), the rate limiter waits for
    at most a given number of seconds (0.1 by default) to see whether the status
    of the connection settles to a stable state ("connected" or "disconnected").
    If the status did not settle, a CONN-INF message is sent with the
    transitioning state. If the status settled to a stable state and it is the
    same as the previous stable state, _no_ message will be sent; otherwise a
    message with the new stable state will be sent.
    """

    @attr.s
    class Entry:
        last_stable_state: ConnectionState = attr.ib()
        settled: Event = attr.ib(factory=Event)

        def notify_settled(self):
            self.settled.set()

        async def wait_to_see_if_settles(self, dispatcher):
            with move_on_after(0.1):
                await self.settled.wait()

            if not self.settled.is_set():
                # State of connection did not settle, dispatch a message on
                # our own
                await dispatcher()

    def __init__(self, factory: Callable[[Iterable[str]], FlockwaveMessage]):
        self._factory = factory
        self._request_tx_queue, self._request_rx_queue = open_memory_channel(256)

    def add_request(
        self, uav_id: str, old_state: ConnectionState, new_state: ConnectionState
    ) -> None:
        self._request_tx_queue.send_nowait((uav_id, old_state, new_state))

    async def run(self, dispatcher, nursery):
        data = {}

        dispatch_tx_queue, dispatch_rx_queue = open_memory_channel(0)

        async def dispatcher_task():
            async with dispatch_rx_queue:
                async for connection_id in dispatch_rx_queue:
                    data.pop(connection_id, None)
                    try:
                        await dispatcher(self._factory((connection_id,)))
                    except Exception:
                        log.exception(
                            f"Error while dispatching messages from {self.__class__.__name__}"
                        )

        nursery.start_soon(dispatcher_task)

        async with dispatch_tx_queue, self._request_rx_queue:
            async for connection_id, old_state, new_state in self._request_rx_queue:
                if new_state.is_transitioning:
                    # New state shows that the connection is currently transitioning
                    if old_state.is_transitioning:
                        # This is weird; we'd better report the new state straight
                        # away
                        send = True
                    else:
                        # Old state is stable; wait to see whether the new state
                        # stabilizes soon
                        send = False
                        if connection_id not in data:
                            data[connection_id] = entry = self.Entry(old_state)
                            nursery.start_soon(
                                entry.wait_to_see_if_settles,
                                partial(dispatch_tx_queue.send, connection_id),
                            )
                else:
                    # New state is stable; this should be reported
                    send = True
                    entry = data.get(connection_id)
                    if entry:
                        # Let the background task know that we have reached a
                        # stable state so no need to wait further
                        entry.notify_settled()
                        if entry.last_stable_state == new_state:
                            # Stable state is the same as the one we have started
                            # from so no need to send a message
                            send = False

                if send:
                    await dispatch_tx_queue.send(connection_id)


class RateLimiters:
    """Helper object for managing the dispatch of rate-limited messages.

    This object holds a reference to a dispatcher function that can be used to
    send messages from the message hub of the application. It also holds a
    mapping from _message group names_ to _rate limiters_.

    There are two operations that you can do with this helper object: you
    can either register a new message group name with a corresponding rate
    limiter, or send a request to one of the registered rate limiters, given
    the corresponding message group name. The request contains additional
    positional and keyword arguments that are interpreted by the rate limiter
    and that the rate limiter uses to decide whether to send a message, and if
    so, _when_ to send it.

    For instance, a default rate limiter registered to UAV-INF messages may
    check whether an UAV-INF message has been sent recently, and if so, it
    may wait up to a given number of milliseconds before it sends another
    UAV-INF message, collecting UAV IDs in a temporary variable while it is
    waiting for the next opportunity to send a message. Another rate limiter
    registered to CONN-INF messages may check the new state of a connection,
    and if the new state is a "transitioning" state, it may wait a short
    period of time before sending a message to see whether the transition
    settles or not.

    Rate limiters must satisfy the RateLimiter_ interface. A GenericRateLimiter_
    is provided for the most common use-case where UAV IDs are collected and
    sent in a single batch with a minimum prescribed delay between batches.
    """

    def __init__(self, dispatcher: Callable[[FlockwaveMessage], Awaitable[None]]):
        """Constructor.

        Parameters:
            dispatcher: the dispatcher function that the rate limiter will use
        """
        self._dispatcher = dispatcher
        self._rate_limiters = {}
        self._running = False

    def register(self, name: str, rate_limiter: RateLimiter) -> None:
        """Registers a new rate limiter corresponding to the message group with
        the given name.

        Parameters:
            name: name of the message group; will be used in `request_to_send()`
                to refer to the rate limiter object associated with this
                group
            rate_limiter: the rate limiter object itself
        """
        if self._running:
            raise RuntimeError(
                "you may not add new rate limiters when the rate limiting tasks "
                "are running"
            )

        self._rate_limiters[name] = rate_limiter

        if hasattr(rate_limiter, "name"):
            rate_limiter.name = name

    def request_to_send(self, name: str, *args, **kwds) -> None:
        """Requests the rate limiter registered with the given name to send
        some messages as soon as the rate limiting rules allow it.

        Additional positional and keyword arguments are forwarded to the
        `add_request()` method of the rate limiter object.

        Parameters:
            name: name of the message group whose rate limiter we are targeting
        """
        self._rate_limiters[name].add_request(*args, **kwds)

    async def run(self):
        self._running = True
        try:
            async with open_nursery() as nursery:
                for entry in self._rate_limiters.values():
                    nursery.start_soon(entry.run, self._dispatcher, nursery)
        finally:
            self._running = False