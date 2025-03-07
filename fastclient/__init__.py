from collections import defaultdict
from multiprocessing import JoinableQueue, Lock, Manager, Process, Value
from multiprocessing.connection import Connection, Pipe
from multiprocessing.connection import wait as wait_for_connection
from queue import Empty
from time import time
from typing import Any, Callable, Iterable, List, Mapping

from fastclient.errors import StoreNotSupportedError, NoListenersError
from fastclient.pools import RequestPool
from fastclient.types import Request, RequestEvent, Response
from threading import Lock

# TODO parameters (rate) and context dicts passed to callbacks


class FastClient():
    """
    Wicked-fast API-client that supports rate-limiting, proxy rotation, token rotation and multiprocessing.
    """

    def __init__(
            self, rate: float, pools: List[RequestPool],
            num_pools: int = 8, max_connections: int = None, use_store: bool = True, use_rps: bool = True) -> None:
        """
        Initialize the client.

        Parameters
        ----------
        rate : float
            The request ratelimit in requests per second
        pools : List[RequestPool]
            The list of request pools to use
        tokens : List[str]
            The list of tokens to use
        auth_mode : str
        auth_field_name : str
            The name of the auth field. (eg. 'Authorization' or 'key')
        num_pools : int, default = 8
            The number of pools to different hosts to keep open
        max_connections : int, default = None
            The maximum number of open connections. If a single request takes longer than one second, this will be a limit to the request speed,
            although setting this higher might cause rate-limiting by the API
        """
        self._rate = rate
        self._pools = pools
        # TODO rate limited token queue
        self._requests = JoinableQueue()
        self._num_pools = num_pools
        self._max_connections = max_connections or self._rate

        self._use_store = use_store
        self._use_rps = use_rps

        # features
        self._ctx_manager = Manager() if self._use_store or self._use_rps else None

        # store
        self._store = self._ctx_manager.dict() if self._use_store else None
        self._store_lock = self._ctx_manager.Lock() if self._use_store else None

        # callbacks
        self._callbacks = defaultdict(list)
        self._callback_registered = False

    def __setitem__(self, key, value):
        if not self._use_store:
            raise StoreNotSupportedError

        self._store_lock.acquire()

        self._store[key] = value

        self._store_lock.release()

    def __getitem__(self, key):
        if not self._use_store:
            raise StoreNotSupportedError

        return self._store[key]

    def on(self, event: RequestEvent, callback: Callable[[Response], None]):
        """
        Set up a callback for an specific event.

        Parameters
        ----------
        event : RequestEvent
            The event to listen for
        callback : Callable[[Response, Mapping[str, Any]], None]
            A callback that receives the response and the context (this is synchronized between callbacks)
        """

        self._callbacks[event].append(callback)
        self._callback_registered = True

    def request(self, request: Request):
        """
        Add a request to the processing queue.

        Parameters
        ----------
        request : Request
            The request object.

        Note
        ----
            This method only stages the requests. Call :meth:`run` to start the processing.
            All responses will trigger their respective callbacks, which have been registered via :meth:`on`.
        """
        self._requests.put(request)

    def run(self):
        if not self._callback_registered:
            raise NoListenersError("No callback registered. Use FastClient.on to register a callback.")

        controllers: List[Process] = []
        rps_counter = None
        # create groups based on the RequestPool's ids
        poolgroups = defaultdict(list)
        pools = []
        for pool in self._pools:
            if pool.id_ is None:
                pools.append(pool)
            else:
                poolgroups[pool.id_].append(pool)

        # rps
        rps = self._ctx_manager.Value('f', 0.0) if self._use_rps else None
        rps10 = self._ctx_manager.Value('I', 0) if self._use_rps else None
        rps1 = self._ctx_manager.Value('I', 0) if self._use_rps else None
        if self._use_rps:
            (recv, send) = Pipe()
        rps_recv = recv if self._use_rps else None
        rps_send = send if self._use_rps else None

        # create ticket connections
        connections = [Pipe() for _ in range(len(pools)+len(poolgroups))]
        ticket_recvs, ticket_sends = ([i for i, _ in connections], [j for _, j in connections])
        del connections

        # create rps counter
        if self._use_rps:
            rps_counter = Process(
                name='FastClient-rps',
                target=FastClient._count_rps,
                args=(rps_recv, rps, rps10, rps1))
            rps_counter.start()

        # create their controllers
        controllers.extend(
            Process(
                name='FastClient-controller', target=FastClient._controller,
                args=((pool,),
                      self._num_pools, self._max_connections, self._requests, ticket_recvs.pop(),
                      self._callbacks, self._use_store, self._store_lock, self._store, self._use_rps,
                      rps_send, rps, rps10, rps1)) for pool in pools)
        del pools

        controllers.extend(
            Process(
                name='FastClient-controller',
                target=FastClient._controller,
                args=(tuple(poolgroup),
                      self._num_pools, self._max_connections, self._requests, ticket_recvs.pop(),
                      self._callbacks, self._use_store, self._store_lock, self._store, self._use_rps, rps_send, rps, rps10, rps1))
            for poolgroup in poolgroups.values())
        del poolgroups

        # start all controllers
        for controller in controllers:
            controller.start()

        # start ticket creation
        tickets = Process(name='FastClient-ticket-manager',
                          target=FastClient._create_tickets, args=(self._rate, ticket_sends))
        tickets.start()

        # now all the processing happens...

        # wait for request queue to be empty
        self._requests.close()
        self._requests.join()

        # stop ticket creation
        tickets.terminate()
        tickets.join()

        # wait for all controllers to finish
        for controller in controllers:
            controller.join()

        if self._use_rps:
            rps_counter.terminate()
            rps_counter.join()

    @staticmethod
    def _controller(pools: Iterable[RequestPool],
                    num_pools: int,
                    max_connections: int,
                    requests: JoinableQueue,
                    tickets: Connection,
                    callbacks: Mapping[RequestEvent, Callable],
                    use_store: bool,
                    store_lock: Lock,
                    store: Mapping[str, Any],
                    use_rps: bool,
                    rps_send: Connection,
                    rps: Value,
                    rps10: Value,
                    rps1: Value):
        # setup all connections to the attached RequestPools
        connections = [pool._setup(num_pools, max_connections) for pool in pools]
        counter = 0
        while True:
            try:
                if counter == 0 and requests.empty():
                    break
                # check if a ticket is available
                if tickets.poll():
                    tickets.recv()
                    # choose the least busy pool
                    pool = min(pools, key=lambda p: p._get_remaining_tasks())
                    # make the request
                    pool._request(requests.get(block=False))
                    counter += 1
                    requests.task_done()
                # check if a response is available
                for connection in wait_for_connection(connections, timeout=0):
                    result = connection.recv()
                    counter -= 1
                    if use_rps:
                        rps_send.send(None)
                    # call the callbacks
                    # TODO if the retry signal is given, put the requests into the queue again
                    # TODO if the stop signal is given, stop
                    # construct context
                    context = {
                        'retry': Callable,
                        'exit': Callable
                    }
                    if use_rps:
                        context.update({
                            'rps': rps.value,
                            'rps10': rps10.value,
                            'rps1': rps1.value
                        })
                    if use_store:
                        store_lock.acquire()
                        context['store'] = store
                    if type(result) == Response:
                        for callback in callbacks[RequestEvent.RESPONSE]:
                            callback(result, context)
                    else:
                        for callback in callbacks[RequestEvent.ERROR]:
                            callback(result, context)
                    if use_store:
                        store_lock.release()
            except Empty:
                pass

    @staticmethod
    def _create_tickets(rate: float, connections: List[Connection]):
        # create tickets at the rate limit per connection and send them to the controllers
        last_tickets = 0
        while True:  # this is meant to be manually terminated
            time_ = time()
            if time_ - last_tickets > 1 / rate:
                for connection in connections:
                    connection.send(None)
                last_tickets = time_

    @staticmethod
    def _count_rps(rps_recv: Connection, rps: Value, rps10: Value, rps1: Value):
        count = 0
        start = time()

        list9 = []
        list1 = []

        while True:
            # wait for response
            rps_recv.recv()

            time_ = time()
            count += 1
            list1.append(time_)
            # move lists
            while len(list1) > 0 and list1[0] < time_-1:
                list9.append(list1.pop(0))
            while len(list9) > 0 and list9[0] < time_-1:
                list9.pop(0)

            # update values
            rps.value = count/(time_-start)
            rps1.value = len(list1)
            rps10.value = len(list1)+len(list9)
