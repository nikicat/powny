import pickle
import time
import logging

from . import const
from . import application
from . import zoo
from . import events


##### Private objects #####
_logger = logging.getLogger(const.LOGGER_NAME)
_collectors = 0


##### Public classes #####
class CollectorThread(application.Thread):
    def __init__(self, client, interval, delay, recycled_priority):
        self._client = client
        self._interval = interval
        self._delay = delay
        self._recycled_priority = recycled_priority
        self._stop_flag = False

        global _collectors
        _collectors += 1
        application.Thread.__init__(self, name="Collector::{collectors:03d}".format(collectors=_collectors))


    ### Public ###

    def stop(self):
        self._stop_flag = True


    ### Private ###

    def run(self):
        while not self._stop_flag:
            self._poll_running()
            self._poll_control()
            if not self._stop_flag:
                time.sleep(self._interval)

    def _poll_running(self):
        for task_id in self._client.get_children(zoo.RUNNING_PATH):
            if self._stop_flag:
                break

            try:
                # XXX: There is no need to control lock
                running_dict = self._client.pget(zoo.join(zoo.RUNNING_PATH, task_id))
                job_id = running_dict[zoo.RUNNING_JOB_ID]
                created = self._client.pget(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS, task_id, zoo.CONTROL_TASK_CREATED))
                recycled = self._client.pget(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS, task_id, zoo.CONTROL_TASK_RECYCLED))
            except zoo.NoNodeError:
                # XXX: Garbage (tasks without jobs)
                lock = self._client.SingleLock(zoo.join(zoo.RUNNING_PATH, task_id, zoo.RUNNING_LOCK))
                if not lock.try_acquire():
                    continue
                self._remove_running(lock, task_id)
                continue

            if max(created or 0, recycled or 0) + self._delay > time.time():
                continue # XXX: Do not grab the new or the respawned tasks

            lock = self._client.SingleLock(zoo.join(zoo.RUNNING_PATH, task_id, zoo.RUNNING_LOCK))
            if not lock.try_acquire():
                continue
            if self._client.pget(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS, task_id, zoo.CONTROL_TASK_FINISHED)) is None:
                self._push_back_running(lock, task_id)
            else:
                self._remove_running(lock, task_id) # TODO: Garbage lifetime

    def _poll_control(self):
        for job_id in self._client.get_children(zoo.CONTROL_JOBS_PATH):
            if self._stop_flag:
                break

            try:
                if not events.is_finished(self._client, job_id):
                    continue
            except zoo.NoNodeError:
                continue
            lock = self._client.SingleLock(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_LOCK))
            if not lock.try_acquire():
                continue

            self._remove_control(lock, job_id)

    ###

    def _push_back_running(self, lock, task_id):
        running_dict = self._client.pget(zoo.join(zoo.RUNNING_PATH, task_id))
        job_id = running_dict[zoo.RUNNING_JOB_ID]
        trans = self._client.transaction()
        trans.delete(zoo.join(zoo.RUNNING_PATH, task_id, zoo.RUNNING_LOCK))
        trans.delete(zoo.join(zoo.RUNNING_PATH, task_id))
        trans.lq_put(zoo.READY_PATH, pickle.dumps({
                zoo.READY_JOB_ID:  job_id,
                zoo.READY_TASK_ID: task_id,
                zoo.READY_HANDLER: running_dict[zoo.RUNNING_HANDLER],
                zoo.READY_STATE:   running_dict[zoo.RUNNING_STATE],
            }))
        trans.pset(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS, task_id, zoo.CONTROL_TASK_RECYCLED), time.time())
        try:
            zoo.check_transaction("push_back_running", trans.commit())
            _logger.info("Pushed back: %s", task_id)
        except zoo.TransactionError:
            _logger.exception("Cannot push-back running")

    def _remove_running(self, lock, task_id):
        trans = self._client.transaction()
        trans.delete(zoo.join(zoo.RUNNING_PATH, task_id, zoo.RUNNING_LOCK))
        trans.delete(zoo.join(zoo.RUNNING_PATH, task_id))
        try:
            zoo.check_transaction("remove_running", trans.commit())
            _logger.info("Running removed: %s", task_id)
        except zoo.TransactionError:
            _logger.exception("Cannot remove running")

    ###

    def _remove_control(self, lock, job_id):
        try:
            trans = self._client.transaction()
            trans.delete(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_PARENTS))
            for task_id in self._client.get_children(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS)):
                for node in (
                        zoo.CONTROL_TASK_ADDED,
                        zoo.CONTROL_TASK_SPLITTED,
                        zoo.CONTROL_TASK_CREATED,
                        zoo.CONTROL_TASK_RECYCLED,
                        zoo.CONTROL_TASK_FINISHED,
                        zoo.CONTROL_TASK_STATUS,
                    ):
                    trans.delete(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS, task_id, node))
                trans.delete(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS, task_id))
            trans.delete(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_TASKS))
            trans.delete(zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_LOCK))
            trans.delete(zoo.join(zoo.CONTROL_JOBS_PATH, job_id))
            with self._client.Lock(zoo.CONTROL_LOCK_PATH):
                cancel_path = zoo.join(zoo.CONTROL_JOBS_PATH, job_id, zoo.CONTROL_CANCEL)
                if self._client.exists(cancel_path) is not None:
                    trans.delete(cancel_path)
                zoo.check_transaction("remove_control", trans.commit())
            _logger.info("Control removed: %s", job_id)
        except (zoo.NoNodeError, zoo.TransactionError):
            _logger.error("Cannot remove control", exc_info=True)

