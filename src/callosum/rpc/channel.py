from __future__ import annotations

import asyncio
import functools
import logging
from typing import (
    Any, Optional, Type, Union,
    Mapping, MutableMapping,
    Tuple, Set, Dict,
    TYPE_CHECKING,
)
import secrets

import aiojobs
from aiotools import aclosing
from async_timeout import timeout
import attr

from ..abc import (
    Sentinel, CLOSED, CANCELLED,
    AbstractChannel,
    AbstractDeserializer, AbstractSerializer,
)
from ..auth import AbstractAuthenticator
from .exceptions import RPCUserError, RPCInternalError
from ..ordering import (
    AsyncResolver, AbstractAsyncScheduler,
    KeySerializedAsyncScheduler, SEQ_BITS,
)
from ..lower import (
    AbstractAddress,
    AbstractConnection,
    AbstractBinder, AbstractConnector,
    BaseTransport,
)
from .message import (
    RPCMessage, RPCMessageTypes,
)
from .types import (
    RequestId,
)
if TYPE_CHECKING:
    from . import FunctionHandler

log = logging.getLogger(__name__)


class Peer(AbstractChannel):
    '''
    Represents a bidirectional connection where both sides can invoke each
    other.

    In Callosum, there is no fixed server or client for a connection.
    Once the connection is established, each peer can become both
    RPC client and RPC server.
    '''

    _connection: Optional[AbstractConnection]
    _deserializer: AbstractDeserializer
    _serializer: AbstractSerializer
    _func_registry: MutableMapping[str, FunctionHandler]
    _outgoing_queue: asyncio.Queue[Union[Sentinel, RPCMessage]]
    _recv_task: Optional[asyncio.Task]
    _send_task: Optional[asyncio.Task]
    _opener: Optional[Union[AbstractBinder, AbstractConnector]]

    # The mapping from (peer ID, client request ID) -> server request ID
    _req_idmap: Dict[Tuple[Any, RequestId], RequestId]

    _log: logging.Logger
    _debug_rpc: bool

    def __init__(
        self, *,
        deserializer: AbstractDeserializer,
        serializer: AbstractSerializer,
        connect: AbstractAddress = None,
        bind: AbstractAddress = None,
        transport: Type[BaseTransport] = None,
        authenticator: AbstractAuthenticator = None,
        transport_opts: Mapping[str, Any] = {},
        scheduler: AbstractAsyncScheduler = None,
        compress: bool = True,
        max_body_size: int = 10 * (2**20),  # 10 MiBytes
        max_concurrency: int = 100,
        execute_timeout: float = None,
        invoke_timeout: float = None,
        debug_rpc: bool = False,
    ) -> None:
        if connect is None and bind is None:
            raise ValueError('You must specify either the connect or bind address.')
        self._connect = connect
        self._bind = bind
        self._opener = None
        self._connection = None
        self._compress = compress
        self._deserializer = deserializer
        self._serializer = serializer
        self._max_concurrency = max_concurrency
        self._exec_timeout = execute_timeout
        self._invoke_timeout = invoke_timeout

        self._scheduler = None
        if transport is None:
            raise ValueError('You must provide a transport class.')
        self._transport = transport(authenticator=authenticator,
                                    transport_opts=transport_opts)
        self._func_registry = {}

        self._client_seq_id = 0
        self._server_seq_id = 0
        self._req_idmap = {}

        # incoming queues
        self._invocation_resolver = AsyncResolver()
        if scheduler is None:
            scheduler = KeySerializedAsyncScheduler()
        self._func_scheduler = scheduler

        # there is only one outgoing queue
        self._outgoing_queue = asyncio.Queue()
        self._recv_task = None
        self._send_task = None

        self._log = logging.getLogger(__name__ + '.Peer')
        self._debug_rpc = debug_rpc

    def handle_function(self, method: str, handler: FunctionHandler) -> None:
        self._func_registry[method] = handler

    def unhandle_function(self, method: str) -> None:
        del self._func_registry[method]

    def _lookup_func(self, method: str) -> FunctionHandler:
        return self._func_registry[method]

    async def _recv_loop(self) -> None:
        '''
        Receive requests and schedule the request handlers.
        '''
        if self._connection is None:
            raise RuntimeError('consumer is not opened yet.')
        if self._scheduler is None:
            self._scheduler = await aiojobs.create_scheduler(
                limit=self._max_concurrency,
            )
        func_tasks: Set[asyncio.Task] = set()
        while True:
            try:
                async with aclosing(self._connection.recv_message()) as agen:
                    async for raw_msg in agen:
                        # TODO: flow-control in transports or peer queues?
                        if raw_msg is None:
                            return
                        request = RPCMessage.decode(raw_msg, self._deserializer)
                        client_request_id = request.request_id
                        server_request_id: Optional[RequestId]
                        if request.msgtype == RPCMessageTypes.FUNCTION:
                            server_seq_id = self._next_server_seq_id()
                            server_request_id = (
                                client_request_id[0],
                                client_request_id[1],
                                server_seq_id,
                            )
                            self._req_idmap[(request.peer_id, client_request_id)] = \
                                server_request_id
                            func_handler = self._lookup_func(request.method)
                            task = asyncio.create_task(self._func_task(
                                server_request_id,
                                request,
                                func_handler,
                            ))
                            func_tasks.add(task)
                            task.add_done_callback(func_tasks.discard)
                            task.add_done_callback(
                                lambda task: self._req_idmap.pop(
                                    (request.peer_id, client_request_id),
                                    None,
                                )
                            )
                        elif request.msgtype == RPCMessageTypes.CANCEL:
                            server_request_id = self._req_idmap.pop(
                                (request.peer_id, client_request_id),
                                None,
                            )
                            if server_request_id is None:
                                continue
                            await asyncio.shield(
                                self._func_scheduler.cancel(server_request_id)
                            )
                        elif request.msgtype in (RPCMessageTypes.RESULT,
                                                 RPCMessageTypes.FAILURE,
                                                 RPCMessageTypes.ERROR):
                            self._invocation_resolver.resolve(
                                client_request_id,
                                request,
                            )
            except asyncio.CancelledError:
                pending_tasks = []
                if func_tasks:
                    for task in func_tasks:
                        if not task.done():
                            task.cancel()
                            pending_tasks.append(task)
                    await asyncio.wait(pending_tasks)
                await asyncio.sleep(0)
                break
            except Exception:
                log.exception('unexpected error')

    async def _send_loop(self) -> None:
        '''
        Fetches and sends out the completed task responses.
        '''
        if self._connection is None:
            raise RuntimeError('consumer is not opened yet.')
        while True:
            try:
                msg = await self._outgoing_queue.get()
                if msg is CLOSED:
                    break
                assert not isinstance(msg, Sentinel)
                await self._connection.send_message(
                    msg.encode(self._serializer))
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception('unexpected error')

    async def __aenter__(self) -> Peer:
        _opener: Union[AbstractBinder, AbstractConnector]
        if self._connect:
            _opener = functools.partial(self._transport.connect,
                                        self._connect)()
        elif self._bind:
            _opener = functools.partial(self._transport.bind,
                                        self._bind)()
        else:
            raise RuntimeError('Misconfigured opener')
        self._opener = _opener
        self._connection = await _opener.__aenter__()
        # NOTE: if we change the order of the following 2 lines of code,
        # then there will be error after "flushall" redis.
        self._send_task = asyncio.create_task(self._send_loop())
        self._recv_task = asyncio.create_task(self._recv_loop())
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._send_task is not None:
            await self._outgoing_queue.put(CLOSED)
            await self._send_task
        if self._recv_task is not None:
            # TODO: pass exception description, e.g. during invoke timeout
            if self._opener is not None:
                await self._opener.__aexit__(*exc_info)
            self._recv_task.cancel()
            await self._recv_task
        if self._scheduler is not None:
            await self._scheduler.close()
        if self._transport is not None:
            await self._transport.close()
        # TODO: add proper cleanup for awaiting on
        # finishing of the "listen" coroutine's spawned tasks

    def _next_client_seq_id(self) -> int:
        current = self._client_seq_id
        self._client_seq_id = (self._client_seq_id + 1) % SEQ_BITS
        return current

    def _next_server_seq_id(self) -> int:
        current = self._server_seq_id
        self._server_seq_id = (self._server_seq_id + 1) % SEQ_BITS
        return current

    async def _func_task(self, server_request_id: Tuple[str, str, int],
                         request: RPCMessage,
                         handler: FunctionHandler) -> None:
        try:
            await self._func_scheduler.schedule(
                server_request_id,
                self._scheduler,
                handler(request))
            try:
                result = await self._func_scheduler.get_fut(server_request_id)
                if result is CANCELLED:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                # exception from user handler => failure
                if self._debug_rpc:
                    self._log.exception('RPC user error')
                response = RPCMessage.failure(request)
            else:
                assert not isinstance(result, Sentinel)
                response = RPCMessage.result(request, result)
            finally:
                self._func_scheduler.cleanup(server_request_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._debug_rpc:
                self._log.exception('RPC internal error')
            # exception from our parts => error
            response = RPCMessage.error(request)
        await self._outgoing_queue.put(response)

    async def invoke(self, method: str, body, *,
                     order_key=None, invoke_timeout=None):
        '''
        Invoke a remote function via the transport connection.
        '''
        if invoke_timeout is None:
            invoke_timeout = self._invoke_timeout
        if order_key is None:
            order_key = secrets.token_hex(8)
        client_seq_id = self._next_client_seq_id()
        try:
            request: RPCMessage
            with timeout(invoke_timeout):
                if callable(body):
                    # The user is using an upper-layer adaptor.
                    async with aclosing(body()) as agen:
                        request = RPCMessage(
                            None,
                            RPCMessageTypes.FUNCTION,
                            method,
                            order_key,
                            client_seq_id,
                            None,
                            await agen.asend(None),
                        )
                        await self._outgoing_queue.put(request)
                        response = await self._invocation_resolver.wait(
                            request.request_id)
                        upper_result = await agen.asend(response.body)
                        try:
                            await agen.asend(None)
                        except StopAsyncIteration:
                            pass
                else:
                    request = RPCMessage(
                        None,
                        RPCMessageTypes.FUNCTION,
                        method,
                        order_key,
                        client_seq_id,
                        None,
                        body,
                    )
                    await self._outgoing_queue.put(request)
                    response = await self._invocation_resolver.wait(
                        request.request_id)
                    upper_result = response.body
            if response.msgtype == RPCMessageTypes.RESULT:
                pass
            elif response.msgtype == RPCMessageTypes.FAILURE:
                raise RPCUserError(*attr.astuple(response.metadata))
            elif response.msgtype == RPCMessageTypes.ERROR:
                raise RPCInternalError(*attr.astuple(response.metadata))
            return upper_result
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # propagate cancellation to the connected peer
            cancel_request = RPCMessage.cancel(request)
            await self._outgoing_queue.put(cancel_request)
            # cancel myself as well
            self._invocation_resolver.cancel(request.request_id)
            raise
        except Exception:
            raise
