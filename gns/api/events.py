from ulib import validators
import ulib.validators.extra

import grpc.server

from .. import stub

from raava import zoo
from raava import events
from raava import rules


##### Public classes #####
class Api(grpc.server.Module):
    def __init__(self, hosts_list):
        self._hosts_list = hosts_list

    @grpc.server.api
    def add(self, **kwargs_dict):
        """ Submit event """
        event_root = rules.EventRoot(kwargs_dict)
        with zoo.Connect(self._hosts_list) as client:
            return events.add(client, event_root, stub.HANDLER.ON_EVENT)

    @grpc.server.api
    def cancel(self, job_id):
        """ Cancel event by job id """
        job_id = validators.extra.validUuid(job_id)
        with zoo.Connect(self._hosts_list) as client:
            events.cancel(client, job_id)

    @grpc.server.api
    def get_jobs(self):
        """ Jobs list """
        with zoo.Connect(self._hosts_list) as client:
            return events.get_jobs(client)

    @grpc.server.api
    def get_finished(self, job_id):
        """ True if the job is finished else false """
        job_id = validators.extra.validUuid(job_id)
        with zoo.Connect(self._hosts_list) as client:
            return events.get_finished(client, job_id)

    @grpc.server.api
    def get_info(self, job_id):
        """ Job info """
        job_id = validators.extra.validUuid(job_id)
        with zoo.Connect(self._hosts_list) as client:
            return events.get_info(client, job_id)
