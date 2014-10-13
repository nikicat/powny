import sys
import argparse
import importlib
import pkgutil
import threading
import logging
import logging.config
import time
import abc

import yaml

import contextlog
from contextlog import get_logger

from ulib import typetools

from .. import tools
from .. import backends
from .. import instance

from .. import optconf
from ..optconf.dumper import make_config_dump
from ..optconf.loaders.yaml import load_file as load_yaml_file

from .. import backdoor


# =====
_config = None


def get_config(check_helpers=()):
    if len(check_helpers) > 0:
        for helper in check_helpers:
            if helper not in _config.helpers.configure:
                raise RuntimeError("Helper '{}' is not configured".format(helper))
    return _config


def init(name, description, args=None):
    global _config
    assert _config is None, "init() has already been called"

    args_parser = argparse.ArgumentParser(prog=name, description=description)
    args_parser.add_argument("-v", "--version", action="version", version=tools.get_version())
    args_parser.add_argument("-c", "--config", dest="config_file_path", default=None, metavar="<file>")
    args_parser.add_argument("-l", "--level", dest="log_level", default=None)
    args_parser.add_argument("-m", "--dump-config", dest="dump_config", action="store_true")
    options = args_parser.parse_args(args)

    # Load configs
    raw = {}
    if options.config_file_path is not None:
        raw = load_yaml_file(options.config_file_path)
    scheme = _get_config_scheme()
    config = optconf.make_config(raw, scheme)

    # Configure info module
    instance.node_name = config.core.node_name
    instance.fqdn = config.core.fqdn

    # Configure logging
    contextlog.patch_logging()
    contextlog.patch_threading()
    logging.captureWarnings(True)
    logging_config = raw.get("logging")
    if logging_config is None:
        logging_config = yaml.load(pkgutil.get_data(__name__, "configs/logging.yaml"))
    if options.log_level is not None:
        logging_config.setdefault("root", {})
        logging_config["root"]["level"] = _valid_log_level(options.log_level)
    logging.config.dictConfig(logging_config)

    # Update scheme for backend opts
    backend_scheme = backends.get_backend_class(config.core.backend).get_options()
    typetools.merge_dicts(scheme, {"backend": backend_scheme})
    config = optconf.make_config(raw, scheme)

    # Update scheme for selected helpers/modules
    for helper_name in config.helpers.configure:
        helper = importlib.import_module(helper_name)
        get_options = getattr(helper, "get_options", None)
        if get_options is None:
            raise RuntimeError("Helper '{}' requires no configuration".format(helper_name))
        typetools.merge_dicts(scheme, {"helpers": get_options()})

    # Provide global configuration for helpers
    _config = optconf.make_config(raw, scheme)

    # Print config dump and exit
    if options.dump_config:
        print(make_config_dump(_config, split_by=((), ("helpers",))))
        sys.exit(0)

    return _config


def init_backdoor(config):
    if config.backdoor.enabled:
        backdoor.start(
            port=config.backdoor.port,
            listen=config.backdoor.listen,
        )


class Application(metaclass=abc.ABCMeta):
    def __init__(self, app_name, config):
        self._app_name = app_name
        self._app_config = config[app_name]
        self._config = config
        self._stop_event = threading.Event()
        self._respawns = 0

    def make_write_app_state(self, app_state):
        instance_info = instance.get_info()
        state = {
            "when":     tools.make_isotime(),
            "instance": instance_info,
            "state": {
                "respawns": self._respawns,
            },
        }
        state["state"].update(app_state)
        return (instance_info["node"], self._app_name, state)

    def stop(self):
        self._stop_event.set()

    def get_backend_object(self):
        return backends.get_backend_class(self._config.core.backend)(**self._config.backend)

    ###

    def run(self):
        logger = get_logger(app=self._app_name)  # App-level context
        init_backdoor(self._config)
        self._respawns = 0
        while not self._stop_event.is_set():
            if self._app_config.max_fails is not None and self._respawns >= self._app_config.max_fails + 1:
                logger.critical("Reached the respawn maximum, exiting...")
                return -1
            try:
                logger.critical("Ready to work")
                self.process()
            except KeyboardInterrupt:
                logger.critical("Received Ctrl+C, exiting...")
                return 0
            except Exception:
                logger.critical("Error in main loop, respawn...", exc_info=True)
                logger.warning("Sleeping %f seconds...", self._app_config.fail_sleep)
                time.sleep(self._app_config.fail_sleep)
                self._respawns += 1
        self.end()
        return 0

    @abc.abstractmethod
    def process(self):
        raise NotImplementedError

    def end(self):
        pass


# =====
def _valid_log_level(arg):
    try:
        return int(arg)
    except ValueError:
        return logging._nameToLevel[arg.upper()]  # pylint: disable=protected-access


def _get_config_scheme():
    scheme = {
        "core": {
            "node_name": optconf.Option(default=None, type=str, help="Node name, must be a unique (uname by default)"),
            "fqdn": optconf.Option(default=None, type=str, help="Machine FQDN (socket.getfqdn() by default)"),
            "backend": optconf.Option(default="zookeeper", help="Backend plugin"),
            "rules_module": optconf.Option(default="rules", help="Name of the rules module/package"),
            "rules_dir": optconf.Option(default="rules", help="Path to rules root"),
        },

        "backdoor": {
            "enabled": optconf.Option(default=False, help="Enable telnet-based backdoor to Python process"),
            "port": optconf.Option(default=2200, help="Backdoor port"),
            "listen": optconf.Option(default=5, help="Listen N clients"),
        },

        "helpers": {
            "configure": optconf.Option(default=[], help="A list of modules that are configured"),
        },

        "api": {
            "backend_connections": optconf.Option(default=5, help="Maximum number of backend connections"),
            "input_limit": optconf.Option(default=5000, help="Limit of the input queue before 503 error"),
            "delete_timeout": optconf.Option(default=15.0, help="Timeout for stop/delete operation"),

            "run": {
                "host": optconf.Option(default="localhost", help="The host for the internal server"),
                "port": optconf.Option(default=80, help="The port for the internal server"),
                "use_threads": optconf.Option(default=True, help="Process each request in a separate thread"),
                "processes": optconf.Option(default=1, help="API maximum worker count"),
                "debug_console": optconf.Option(default=True, help="Open interactive console with exception "
                                                                   "context in browser"),
            },
        },

        "worker": {
            "max_jobs_sleep": optconf.Option(default=1, help="If we have reached the maximum concurrent jobs - "
                                                             "the process goes to sleep (seconds)"),
            "max_jobs": optconf.Option(default=100, help="The maximum number of job processes"),
        },

        "collector": {
            "done_lifetime": optconf.Option(default=60, help="Seconds to wait before deleting completed job"),
        },
    }
    for app in ("worker", "collector"):
        scheme[app].update({
            "max_fails": optconf.Option(default=None, type=int, help="Number of failures after which the program "
                                                                     "terminates"),
            "fail_sleep": optconf.Option(default=5, help="If processing fails, sleep for awhile and restart (seconds)"),
            "empty_sleep": optconf.Option(default=1, help="Interval after which process will sleep when "
                                                          "there are no jobs (seconds)"),
        })
    return scheme
