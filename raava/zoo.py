"""
    === ZooKeeper nodes scheme ===

    /input           # LockingQueue(); Queue in which to place the data from events.add().

    /control         # Lock(); Temporary tasks data, counters and the control interface.
    /control/lock    # Global lock for the control interface. Used in events.get_info(),
                     # get_finished() to obtain consistent data about jobs.

    /control/jobs/<job_uuid>             # Job data.
    /control/jobs/<job_uuid>/lock        # SingleLock(); This lock is used by collector when searching finished tasks.
    /control/jobs/<job_uuid>/cancel      # If this node exists, the job will be stopped.
    /control/jobs/<job_uuid>/version     # The rules HEAD, which is used when creating the job.
    /control/jobs/<job_uuid>/parents     # List with parent jobs.
    /control/jobs/<job_uuid>/added       # Time when the job was added to /input.
    /control/jobs/<job_uuid>/splitted    # Time when the job was processed by splitter.

    /control/jobs/<job_uuid>/tasks/<task_uuid>             # The task data.
    /control/jobs/<job_uuid>/tasks/<task_uuid>/created     # Time when the task was started for the first time.
    /control/jobs/<job_uuid>/tasks/<task_uuid>/recycled    # If the task has fallen, collector
                                                           # put it in /ready, setting this timestamp.
    /control/jobs/<job_uuid>/tasks/<task_uuid>/finished    # The task completion time.
    /control/jobs/<job_uuid>/tasks/<task_uuid>/status      # The task status (new/ready/finished).
    /control/jobs/<job_uuid>/tasks/<task_uuid>/stack       # Stack of the task.
    /control/jobs/<job_uuid>/tasks/<task_uuid>/exc         # If the handler is crashed, contains an exception as string.

    /ready    # LockingQueue(); Queue for worker with the ready to run tasks.

    /running
    /running/<task_uuid>         # Here are details of running tasks: a reference to the function, the pickled stack,
                                 # etc. A signle node.
    /running/<task_uuid>/lock    # SingleLock(); This lock is used by collector when searching fallen tasks.

    /core                 # Common system section.
    /core/jobs_counter    # Incremental counter for input jobs/events.

    /user    # Section for user data.
"""


import pickle
import functools
import threading
import logging

import kazoo.client
import kazoo.protocol.paths
import kazoo.protocol.states
from kazoo.exceptions import * # pylint: disable=W0401,W0614
from kazoo.protocol.paths import join # pylint: disable=W0611


##### Public constants #####
INPUT_PATH   = "/input"
CONTROL_PATH = "/control"
READY_PATH   = "/ready"
RUNNING_PATH = "/running"
CORE_PATH    = "/core"
USER_PATH    = "/user"

INPUT_JOB_ID = "job_id"
INPUT_EVENT  = "event"

LOCK = "lock"

CONTROL_VERSION        = "version"
CONTROL_PARENTS        = "parents"
CONTROL_ADDED          = "added"
CONTROL_SPLITTED       = "splitted"
CONTROL_JOBS           = "jobs"
CONTROL_JOBS_PATH      = join(CONTROL_PATH, CONTROL_JOBS)
CONTROL_TASKS          = "tasks"
CONTROL_TASK_CREATED   = "created"
CONTROL_TASK_RECYCLED  = "recycled"
CONTROL_TASK_FINISHED  = "finished"
CONTROL_TASK_STATUS    = "status"
CONTROL_TASK_STACK     = "stack"
CONTROL_TASK_EXC       = "exc"
CONTROL_CANCEL         = "cancel"
CONTROL_LOCK_PATH      = join(CONTROL_PATH, LOCK)

READY_JOB_ID   = INPUT_JOB_ID
READY_TASK_ID  = "task_id"
READY_HANDLER  = "handler"
READY_STATE    = "state"

RUNNING_JOB_ID  = READY_JOB_ID
RUNNING_HANDLER = READY_HANDLER
RUNNING_STATE   = READY_STATE

JOBS_COUNTER = "jobs_counter"
JOBS_COUNTER_PATH = join(CORE_PATH, JOBS_COUNTER)

class TASK_STATUS:
    NEW      = "new"
    READY    = "ready"
    FINISHED = "finished"


##### Private objects #####
_logger = logging.getLogger(__name__)


##### Exceptions #####
class TransactionError(KazooException):
    pass


##### Public methods #####
def connect(zoo_nodes, timeout, randomize_hosts):
    hosts = ",".join(zoo_nodes)
    client = Client(hosts=hosts, timeout=timeout, randomize_hosts=randomize_hosts)
    client.start()
    _logger.info("Started zookeeper client on hosts: %s", hosts)
    return client

def init(client, fatal = False):
    for path in (INPUT_PATH, READY_PATH, RUNNING_PATH, CONTROL_JOBS_PATH, JOBS_COUNTER_PATH, USER_PATH):
        try:
            client.create(path, makepath=True)
            _logger.info("Created zoo path: %s", path)
        except NodeExistsError:
            level = ( logging.ERROR if fatal else logging.DEBUG )
            _logger.log(level, "Zoo path is already exists: %s", path)
            if fatal:
                raise

    # Some of our code does not use the API of AbortableLockingQueue(), and puts the data in the queue by using
    # transactions. Because transactions can not do CAS (to prepare the tree nodes), we must be sure that
    # the right tree was set up in advance.
    client.LockingQueue(INPUT_PATH)._ensure_paths() # pylint: disable=W0212
    client.LockingQueue(READY_PATH)._ensure_paths() # pylint: disable=W0212

    # To Lock() to do it is not necessary. This line is added to show the location in node structure.
    client.Lock(CONTROL_LOCK_PATH)._ensure_path() # pylint: disable=W0212

def drop(client, fatal = False):
    for path in (INPUT_PATH, READY_PATH, RUNNING_PATH, CONTROL_PATH, CORE_PATH, USER_PATH):
        try:
            client.delete(path, recursive=True)
            _logger.info("Removed zoo path: %s", path)
        except NoNodeError:
            level = ( logging.ERROR if fatal else logging.DEBUG )
            _logger.log(level, "Zoo path is already exists: %s", path)
            if fatal:
                raise


###
def check_transaction(name, results, pairs = None):
    ok = True
    for (index, result) in enumerate(results):
        if isinstance(result, Exception):
            ok = False
            if pairs is not None:
                _logger.error("Failed the part of transaction \"%s\": %s=%s; err=%s",
                    name,
                    pairs[index][0], # Node
                    pairs[index][1], # Data
                    result.__class__.__name__,
                )
    if not ok:
        if pairs is None:
            _logger.error("Failed transaction \"%s\": %s", name, results)
        raise TransactionError("Failed transaction: %s" % (name))


##### Public classes #####
class SingleLock:
    def __init__(self, client, path):
        self._client = client
        self._path = path

    def try_acquire(self, fatal = False):
        try:
            self._client.create(self._path, ephemeral=True)
            return True
        except NoNodeError:
            if fatal:
                raise
            return False
        except NodeExistsError:
            return False

    def acquire(self, fatal = True):
        while not self.try_acquire(fatal):
            wait = threading.Event()
            def watcher(_) :
                wait.set()
            if self._client.exists(self._path, watch=watcher) is not None:
                wait.wait()

    def release(self):
        try:
            self._client.delete(self._path)
        except NoNodeError:
            pass

    def __enter__(self):
        self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()

class IncrementalCounter:
    def __init__(self, client, path):
        self._client = client
        self._path = path

    def increment(self):
        with self._client.Lock(join(self._path, LOCK)):
            try:
                value = self._client.pget(self._path)
            except (NoNodeError, EOFError):
                value = 0
            self._client.pset(self._path, value + 1)
        return value

class AbortableLockingQueue(kazoo.recipe.queue.LockingQueue):
    def get(self, poll_every=0.1):
        self._ensure_paths()
        if not self.processing_element is None:
            return self.processing_element[1]
        else:
            self._abort = False
            return self._inner_get(poll_every)

    def abort_get(self):
        self._abort = True # pylint: disable=W0201

    def _inner_get(self, poll_every):
        # XXX: Partial copypaste from kazoo-1.3.1-py3.2.egg/kazoo/recipe/queue.py:252
        # In my implementation, get() does not accept "timeout" argument and waits until
        # the object appears in the queue. Waiting can interrupt by abort_get().
        # Frequent calls of LockingQueue.get(timeout) lead to memory leaks, if data in the
        # queue rarely appear. This is due to the fact that more and more instances of
        # check_for_updates() registered as watchers.

        flag = self.client.handler.event_object()
        lock = self.client.handler.lock_object()
        canceled = False
        value = []

        def check_for_updates(event):
            if not event is None and event.type != kazoo.protocol.states.EventType.CHILD:
                return
            with lock:
                if canceled or flag.isSet():
                    return
                values = self.client.retry(self.client.get_children,
                    self._entries_path,
                    check_for_updates)
                taken = self.client.retry(self.client.get_children,
                    self._lock_path,
                    check_for_updates)
                available = self._filter_locked(values, taken)
                if len(available) > 0:
                    ret = self._take(available[0])
                    if not ret is None:
                        # By this time, no one took the task
                        value.append(ret)
                        flag.set()

        check_for_updates(None)
        retval = None
        while not self._abort:
            flag.wait(poll_every)
            if flag.isSet():
                self._abort = True
        with lock:
            canceled = True
            if len(value) > 0:
                # We successfully locked an entry
                self.processing_element = value[0]
                retval = value[0][1]
        return retval

class Client(kazoo.client.KazooClient): # pylint: disable=R0904
    def __init__(self, *args, **kwargs):
        self.SingleLock = functools.partial(SingleLock, self)
        self.IncrementalCounter = functools.partial(IncrementalCounter, self)
        self.AbortableLockingQueue = functools.partial(AbortableLockingQueue, self)
        kazoo.client.KazooClient.__init__(self, *args, **kwargs)

    def pget(self, path):
        return pickle.loads(self.get(path)[0])

    def pset(self, path, value):
        return self.set(path, pickle.dumps(value))

    def pcreate(self, path, value):
        return self.create(path, pickle.dumps(value))

    def transaction(self):
        return TransactionRequest(self)

class TransactionRequest(kazoo.client.TransactionRequest):
    def lq_put(self, queue_path, data, priority = 100):
        if isinstance(queue_path, (list, tuple)):
            queue_path = kazoo.protocol.paths.join(*queue_path)
        self.create("{path}/entries/entry-{priority:03d}-".format(
                path=queue_path,
                priority=priority,
            ), data, sequence=True)

    def pset(self, path, value):
        return self.set_data(path, pickle.dumps(value))

    def pcreate(self, path, value):
        return self.create(path, pickle.dumps(value))

