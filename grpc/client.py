import socket
import urllib.request
import urllib.error
import json

from . import const


##### Exceptions #####
class ApiError(Exception):
    def __init__(self, name, text):
        self._name = name
        self._text = text

    def get_name(self):
        return self._name

    def get_text(self):
        return self._text

    def __str__(self):
        return "%s: %s" % (self._name, self._text)


##### Public classes #####
class Proxy:
    def __init__(self, url, timeout = socket._GLOBAL_DEFAULT_TIMEOUT): # pylint: disable=W0212
        self._url = url
        self._timeout = timeout
        self._inspect_cache_dict = {}

    def __getattr__(self, name):
        return _Object(self, name)

    def __call__(self, path, *args_tuple, **kwargs_dict):
        inspected_tuple = self._inspect_args(path)
        args_dict = {
            inspected_tuple[count]: args_tuple[count]
            for count in range(len(args_tuple))
        }
        args_dict.update(kwargs_dict)

        request = urllib.request.Request(
            "%s/%s" % (self._url, path),
            json.dumps(args_dict).encode(),
            { "Content-Type": "application/json" },
        )
        opener = urllib.request.build_opener()
        try:
            response = opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as err:
            if err.code == 500:
                result_dict = json.loads(err.read().decode())
                raise ApiError(*result_dict[const.API_EXCEPTION])
            else:
                raise
        result_dict = json.loads(response.read().decode())
        return result_dict[const.API_RETVAL]

    def _inspect_args(self, path):
        if path in self._inspect_cache_dict:
            return self._inspect_cache_dict[path]
        opener = urllib.request.build_opener()
        response = opener.open("%s/%s?action=%s" % (self._url, path, const.ACTION.INSPECT), timeout=self._timeout)
        args_tuple = tuple(json.loads(response.read().decode())[const.ARGS_ALL])
        self._inspect_cache_dict[path] = args_tuple
        return args_tuple


##### Private classes #####
class _Object:
    def __init__(self, proxy, path):
        self._proxy = proxy
        self._path = path

    def __getattr__(self, name):
        return _Object(self._proxy, "%s/%s" % (self._path, name))

    def __call__(self, *args_tuple, **kwargs_dict):
        return self._proxy(self._path, *args_tuple, **kwargs_dict)
