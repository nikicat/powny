import abc

import flask


# =====
class ApiError(Exception):
    def __init__(self, code, message, result=None):
        super(ApiError, self).__init__()
        self.code = code
        self.message = message
        self.result = result


class Resource(metaclass=abc.ABCMeta):
    name = "<Resource>"
    dynamic = False
    methods = ("GET",)
    docstring = None

    def handler(self, **kwargs):
        try:
            (result, message) = self.process_request(**kwargs)
            return {
                "status":  "ok",
                "message": message,
                "result":  result
            }
        except ApiError as err:
            result = {
                "status":  "error",
                "message": err.message,
                "result":  err.result,
            }
            return (result, err.code)
        except Exception as err:
            message = "{}: {}".format(type(err).__name__, str(err))
            if hasattr(err, "__module__"):
                message = "{}.{}".format(err.__module__, message)
            result = {
                "status": "error",
                "message": message,
                "result": None,
            }
            return (result, 500)

    @abc.abstractmethod
    def process_request(self, **kwargs):
        raise NotImplementedError


def get_url_for(resource_class, **kwargs):
    return flask.request.host_url.rstrip("/") + flask.url_for(resource_class.name, **kwargs)
