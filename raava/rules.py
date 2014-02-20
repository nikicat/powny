import copy
import logging

from . import const


##### Private objects #####
_logger = logging.getLogger(const.LOGGER_NAME)


##### Public constants #####
class EXTRA:
    HANDLER = "handler"
    JOB_ID  = "job_id"


##### Private constants #####
_DISABLE_HANDLER = "_disable_handler"

class _FILTER:
    EVENT = "event_filters"
    EXTRA = "extra_filters"


##### Exceptions #####
class ComparsionError(Exception):
    pass


##### Public methods #####
def _make_matcher(filters_type):
    def matcher(**filters):
        def make_handler(handler):
            setattr(handler, filters_type, filters)
            for (key, comparator) in tuple(filters.items()):
                if not isinstance(comparator, AbstractComparator):
                    comparator = EqComparator(comparator)
                    filters[key] = comparator
                comparator.set_handler(handler)
            return handler
        return make_handler
    return matcher
match_event = _make_matcher(_FILTER.EVENT)
match_extra = _make_matcher(_FILTER.EXTRA)

def disable_handler(handler):
    setattr(handler, _DISABLE_HANDLER, None)
    return handler

###
def get_handlers(event_root, handlers):
    handler_type = event_root.get_extra()[EXTRA.HANDLER]
    job_id = event_root.get_extra()[EXTRA.JOB_ID]
    selected_set = set()
    for handler in handlers.get(handler_type, set()):
        if hasattr(handler, _DISABLE_HANDLER):
            _logger.debug("Passed disabled handler: %s.%s", handler.__module__, handler.__name__)
            continue
        event_filters = getattr(handler, _FILTER.EVENT, {})
        extra_filters = getattr(handler, _FILTER.EXTRA, {})
        if len(event_filters) + len(extra_filters) == 0:
            selected_set.add(handler)
            _logger.debug("Applied: %s --> %s.%s", job_id, handler.__module__, handler.__name__)
        else:
            if ( _check_match(job_id, handler, event_filters, event_root) and
                _check_match(job_id, handler, extra_filters, event_root.get_extra()) ):
                selected_set.add(handler)
                _logger.debug("Applied: %s --> %s.%s", job_id, handler.__module__, handler.__name__)
    return selected_set


##### Private methods #####
def _compare(comparator, value):
    if isinstance(comparator, AbstractComparator):
        try:
            return comparator.compare(value)
        except Exception:
            raise ComparsionError("Invalid operands: %s vs. %s" % (repr(value), repr(comparator.get_operand())))
    else:
        return ( comparator == value )

def _check_match(job_id, handler, filters, event):
    for (key, comparator) in filters.items():
        try:
            if not (key in event and _compare(comparator, event[key])):
                _logger.debug("Event %s/%s: not matched with %s(%s); handler: %s.%s",
                    job_id, key, comparator.__class__.__name__, repr(comparator.get_operand()), handler.__module__, handler.__name__)
                return False
        except ComparsionError as err:
            _logger.debug("Matching error on %s/%s: %s: %s; handler: %s.%s",
                job_id, key, comparator.__class__.__name__, str(err), handler.__module__, handler.__name__)
            return False
    return True


##### Public classes #####
class EventRoot(dict):
    def __init__(self, *args, **kwargs):
        self._extra_attrs = kwargs.pop("extra", {})
        dict.__init__(self, *args, **kwargs)

    def copy(self):
        return copy.deepcopy(self)

    def get_extra(self):
        return self._extra_attrs

    def set_extra(self, extra):
        self._extra_attrs = extra


###
class AbstractComparator:
    def __init__(self, operand):
        self._operand = operand
        self._handler = None

    def set_handler(self, handler):
        self._handler = handler

    def get_operand(self):
        return self._operand

    def compare(self, value):
        raise NotImplementedError

class EqComparator(AbstractComparator): # Default comparsion method
    def compare(self, value):
        return ( value == self._operand )

