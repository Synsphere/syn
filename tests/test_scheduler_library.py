from scheduler.Scheduler import Scheduler


def double(value: int) -> int:
    return value * 2


def test_async_process_scheduler_executes_in_a_process() -> None:
    scheduler = Scheduler(dynamic=False)
    assert scheduler.map_blocking(double, [(3,)]) == [6]
