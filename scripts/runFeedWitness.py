from __future__ import annotations

import json

from scheduler.Scheduler import Scheduler
from sqlalchemy import text

from syn import createEngine, defaultJobHandlers, loadConfig
from syn.feed_jobs import FEED_WITNESS_HANDLER


def main() -> None:
    config = loadConfig()
    engine = createEngine(
        config.database.url,
        echo=config.database.echo,
        migrate=False,
    )
    try:
        with engine.connect() as connection:
            feedCount = connection.scalar(
                text(
                    '''
                    SELECT count(DISTINCT "objectRef")
                    FROM "objectTypesInherited"
                    WHERE "typeRef" = 'syn://type/feed'::"typeRef"
                    '''
                )
            )
        print(f"Eligible feeds: {feedCount}", flush=True)
    finally:
        engine.dispose()

    handler = defaultJobHandlers(config.database.url)[FEED_WITNESS_HANDLER]
    scheduler = Scheduler(dynamic=False, raise_exceptions=True)
    result = scheduler.map_blocking(handler, [(100, 50, 30, 10_000_000, True)])[0]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
