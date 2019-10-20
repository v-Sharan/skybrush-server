from contextlib import ExitStack

from tinyrpc import InvalidRequestError
from tinyrpc.dispatch import RPCDispatcher
from trio import open_nursery, sleep_forever
from trio.abc import Stream

from flockwave.connections import create_connection, StreamWrapperConnection
from flockwave.channels import MessageChannel
from flockwave.encoders.jsonrpc import JSONRPCEncoder
from flockwave.listeners import create_listener
from flockwave.logger import Logger
from flockwave.parsers.jsonrpc import JSONRPCParser, RPCMessage
from flockwave.server.model import ConnectionPurpose
from flockwave.server.model.uav import PassiveUAVDriver

from ..base import UAVExtensionBase

from .rpc import DockRPCServer

############################################################################


def create_rpc_message_channel(
    stream: Stream, log: Logger
) -> MessageChannel[RPCMessage]:
    """Creates a bidirectional Trio-style channel that reads data from and
    writes data to the given Trio stream, and does the parsing of JSON-RPC
    messages automatically.

    Parameters:
        connection: the connection to read data from and write data to
        log: the logger on which any error messages and warnings should be logged
    """
    rpc_parser = JSONRPCParser()
    rpc_encoder = JSONRPCEncoder()

    def encoder(data: RPCMessage) -> bytes:
        return rpc_encoder.encode(data) + b"\n"

    return MessageChannel(
        StreamWrapperConnection(stream), parser=rpc_parser.feed, encoder=encoder
    )


############################################################################


class DockExtension(UAVExtensionBase):
    """Extension that implements support for CollMot Robotics' docking station."""

    def __init__(self):
        """Constructor."""
        super(DockExtension, self).__init__()
        self._connection = create_connection("dummy")
        self._id_format = None
        self._device_to_uav_id = {}

        self._dispatcher = RPCDispatcher()
        self._dispatcher.register_instance(DockRPCServer())

        self._current_stream = None

    def _create_driver(self):
        return PassiveUAVDriver()

    def configure(self, configuration):
        """Loads the extension."""
        self._id_format = configuration.get("id_format", "DOCK:{0}")

    async def handle_connection(self, stream):
        """Handles a connection attempt from a single client.

        Parameters:
            stream (SocketStream): a Trio socket stream that we can use to
                communicate with the client
        """
        channel = create_rpc_message_channel(stream)

        try:
            async for message in channel:
                if hasattr(message, "method"):
                    # TODO(ntamas): do it in a separate task
                    response = self._dispatcher.dispatch(message)
                    if response:
                        await channel.send(response)
                elif isinstance(message, list):
                    self.log.warn("Batched requests not supported; dropping message")
                else:
                    # TODO(ntamas): handle responses
                    self.log.warn("Only RPC requests are supported; dropping message")
        except InvalidRequestError:
            self.log.error("Invalid RPC request, closing connection.")

    async def handle_connection_safely(self, stream):
        """Handles a connection attempt from a single client, ensuring
        that exceptions do not propagate through.

        Parameters:
            stream (SocketStream): a Trio socket stream that we can use to
                communicate with the client
        """
        if self._current_stream is not None:
            self.log.warn(
                "Only one connection is supported; rejecting connection attempt"
            )
            return

        try:
            self._current_stream = stream
            async with self._connection:
                return await self.handle_connection(stream)
        except Exception as ex:
            # Exceptions raised during a connection are caught and logged here;
            # we do not let the main task itself crash because of them
            self.log.exception(ex)
        finally:
            self._current_stream = None

    async def run(self, app, configuration, logger):
        listener = configuration.get("listener")
        if not listener:
            logger.warn("No listener specified; dock extension disabled")
            return

        async with open_nursery() as nursery:
            listener = create_listener(listener)
            listener.handler = self.handle_connection_safely
            listener.nursery = nursery

            with ExitStack() as stack:
                stack.enter_context(
                    app.connection_registry.use(
                        self._connection,
                        "Dock",
                        "Docking station",
                        # TODO(ntamas): use ConnectionPurpose.dock
                        purpose=ConnectionPurpose.other,
                    )
                )

                async with listener:
                    await sleep_forever()


construct = DockExtension