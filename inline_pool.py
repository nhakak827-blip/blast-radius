"""Expose arithmetic work sent to process pools to the fixture coverage recorder."""
from concurrent.futures import Executor, Future
import concurrent.futures


class InlineExecutor(Executor):
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, function, /, *args, **kwargs):
        result = Future()
        try:
            result.set_result(function(*args, **kwargs))
        except Exception as error:
            result.set_exception(error)
        return result


def install():
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
